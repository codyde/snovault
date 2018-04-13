import datetime
import logging
import time
import copy
import pytz

from pyramid.view import view_config
from urllib3.exceptions import ReadTimeoutError
from sqlalchemy.exc import StatementError

from elasticsearch.exceptions import (
    ConflictError,
    ConnectionError as ES_ConnectionError,
    TransportError,
)

from snovault import (
    DBSESSION,
    STORAGE
)
from snovault.storage import (
    TransactionRecord,
)
from .interfaces import (
    ELASTIC_SEARCH,
    INDEXER
)
from .indexer_state import (
    IndexerState,
    all_uuids,
    SEARCH_MAX
)


ES_LOGGER = logging.getLogger("elasticsearch")
ES_LOGGER.setLevel(logging.ERROR)
LOG = logging.getLogger(__name__)
MAX_CLAUSES_FOR_ES = 8192


def includeme(config):
    config.add_route('index', '/index')
    config.scan(__name__)
    registry = config.registry
    registry[INDEXER] = Indexer(registry)


def get_related_uuids(request, elastic_search, updated, renamed):
    '''Returns (set of uuids, True if all_uuids)'''
    updated_count = len(updated)
    renamed_count = len(renamed)
    if (updated_count + renamed_count) > MAX_CLAUSES_FOR_ES:
        return (all_uuids(request.registry), True)
    elif (updated_count + renamed_count) == 0:
        return (set(), False)

    elastic_search.indices.refresh('_all')
    res = elastic_search.search(index='_all', size=SEARCH_MAX, request_timeout=60, body={
        'query': {
            'bool': {
                'should': [
                    {
                        'terms': {
                            'embedded_uuids': updated,
                            '_cache': False,
                        },
                    },
                    {
                        'terms': {
                            'linked_uuids': renamed,
                            '_cache': False,
                        },
                    },
                ],
            },
        },
        '_source': False,
    })

    if res['hits']['total'] > SEARCH_MAX:
        return (all_uuids(request.registry), True)

    related_set = {hit['_id'] for hit in res['hits']['hits']}

    return (related_set, False)


@view_config(route_name='index', request_method='POST', permission="index")
def index(request):
    # pylint: disable=too-many-locals
    # pylint: disable=too-many-branches
    # pylint: disable=too-many-statements
    print('index view_config')
    reg_index_str = request.registry.settings['snovault.elasticsearch.index']
    # Setting request.datastore here only works because routed views are not traversed.
    request.datastore = 'database'

    record = request.json.get('record', False)
    dry_run = request.json.get('dry_run', False)
    recovery = request.json.get('recovery', False)

    elastic_search = request.registry[ELASTIC_SEARCH]
    indexer = request.registry[INDEXER]
    session = request.registry[DBSESSION]()

    connection = session.connection()

    first_txn = None
    snapshot_id = None
    restart = False
    invalidated = []
    xmin = -1

    # Currently 2 possible followup indexers (base.ini [set stage_for_followup = vis_indexer, region_indexer])
    stage_for_followup = list(request.registry.settings.get("stage_for_followup", '').replace(' ', '').split(','))
    print('indexer', 'view config start', stage_for_followup)

    # May have undone uuids from prior cycle
    state = IndexerState(elastic_search, reg_index_str, followups=stage_for_followup)

    (xmin, invalidated, restart) = state.priority_cycle(request)
    # OPTIONAL: restart support
    if restart:  # Currently not bothering with restart!!!
        xmin = -1
        invalidated = []
    # OPTIONAL: restart support

    result = state.get_initial_state()  # get after checking priority!

    if xmin == -1 or not invalidated:
        xmin = get_current_xmin(request)

        last_xmin = None
        if 'last_xmin' in request.json:
            last_xmin = request.json['last_xmin']
        else:
            status = elastic_search.get(index=reg_index_str, doc_type='meta', id='indexing', ignore=[400, 404])
            if status['found'] and 'xmin' in status['_source']:
                last_xmin = status['_source']['xmin']
        if last_xmin is None:  # still!
            if 'last_xmin' in result:
                last_xmin = result['last_xmin']
            elif 'xmin' in result and result['xmin'] < xmin:
                last_xmin = result['state']

        result.update(
            xmin=xmin,
            last_xmin=last_xmin,
        )

    if len(invalidated) > SEARCH_MAX:  # Priority cycle already set up
        flush = True
    else:

        flush = False
        if last_xmin is None:
            result['types'] = types = request.json.get('types', None)
            invalidated = list(all_uuids(request.registry, types))
            flush = True
        else:
            txns = session.query(TransactionRecord).filter(
                TransactionRecord.xid >= last_xmin,
            )

            invalidated = set(invalidated)  # not empty if API index request occurred
            updated = set()
            renamed = set()
            max_xid = 0
            txn_count = 0
            for txn in txns.all():
                txn_count += 1
                max_xid = max(max_xid, txn.xid)
                if first_txn is None:
                    first_txn = txn.timestamp
                else:
                    first_txn = min(first_txn, txn.timestamp)
                renamed.update(txn.data.get('renamed', ()))
                updated.update(txn.data.get('updated', ()))

            if invalidated:        # reindex requested, treat like updated
                updated |= invalidated

            result['txn_count'] = txn_count
            if txn_count == 0 and not invalidated:
                state.send_notices()
                return result

            (related_set, full_reindex) = get_related_uuids(request, elastic_search, updated, renamed)
            if full_reindex:
                invalidated = related_set
                flush = True
            else:
                invalidated = related_set | updated
                result.update(
                    max_xid=max_xid,
                    renamed=renamed,
                    updated=updated,
                    referencing=len(related_set),
                    invalidated=len(invalidated),
                    txn_count=txn_count
                )
                if first_txn is not None:
                    result['first_txn_timestamp'] = first_txn.isoformat()

            if invalidated and not dry_run:
                # Exporting a snapshot mints a new xid, so only do so when required.
                # Not yet possible to export a snapshot on a standby server:
                # http://www.postgresql.org/message-id/CAHGQGwEtJCeHUB6KzaiJ6ndvx6EFsidTGnuLwJ1itwVH0EJTOA@mail.gmail.com
                if snapshot_id is None and not recovery:
                    snapshot_id = connection.execute('SELECT pg_export_snapshot();').scalar()

    if invalidated and not dry_run:
        invalidated = invalidated[:1000]
        print('starting to updates', str(len(invalidated)))
        start_time = time.time()
        result = process_uuids(
            request, invalidated, stage_for_followup,
            xmin, state, result, indexer
        )
        print('update over', '%0.6f' % (time.time() - start_time))
        result = post_process_uuids(elastic_search, reg_index_str, result, flush, record)
    if first_txn is not None:
        result['txn_lag'] = str(datetime.datetime.now(pytz.utc) - first_txn)
    state.send_notices()
    return result


def process_uuids(
        request,
        invalidated,
        stage_for_followup,
        xmin,
        state,
        result,
        indexer,
    ):
    # pylint: disable=too-many-arguments
    if stage_for_followup:
        # Note: undones should be added before, because those uuids will (hopefully) be indexed in this cycle
        state.prep_for_followup(xmin, invalidated)
    result = state.start_cycle(invalidated, result)
    # Do the work...
    errors = indexer.update_objects(request, invalidated, xmin)
    result = state.finish_cycle(result, errors)
    if errors:
        result['errors'] = errors
    return result


def post_process_uuids(elastic_search, reg_index_str, result, flush, record):
    if record:
        try:
            elastic_search.index(index=reg_index_str, doc_type='meta', body=result, id='indexing')
        except Exception as ecp:  # pylint: disable=broad-except
            print(ecp)
            error_messages = copy.deepcopy(result['errors'])
            del result['errors']
            elastic_search.index(index=reg_index_str, doc_type='meta', body=result, id='indexing')
            for item in error_messages:
                if 'error_message' in item:
                    LOG.error(
                        'Indexing error for %s, error message: %s',
                        item['uuid'],
                        item['error_message']
                    )
                    item['error_message'] = "Error occured during indexing, check the logs"
            result['errors'] = error_messages
    elastic_search.indices.refresh('_all')
    if flush:
        try:
            elastic_search.indices.flush_synced(index='_all')  # Faster recovery on ES restart
        except ConflictError:
            pass
    return result


def get_current_xmin(request):
    session = request.registry[DBSESSION]()
    connection = session.connection()
    recovery = request.json.get('recovery', False)

    # http://www.postgresql.org/docs/9.3/static/functions-info.html#FUNCTIONS-TXID-SNAPSHOT
    if recovery:
        query = connection.execute(
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED, READ ONLY;"
            "SELECT txid_snapshot_xmin(txid_current_snapshot());"
        )
    else:
        query = connection.execute(
            "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE, READ ONLY, DEFERRABLE;"
            "SELECT txid_snapshot_xmin(txid_current_snapshot());"
        )
    # DEFERRABLE prevents query cancelling due to conflicts but requires SERIALIZABLE mode
    # which is not available in recovery.
    xmin = query.scalar()  # lowest xid that is still in progress
    return xmin

class Indexer(object):
    def __init__(self, registry):
        self.elastic_search = registry[ELASTIC_SEARCH]
        self.esstorage = registry[STORAGE]
        self.index = registry.settings['snovault.elasticsearch.index']

    def update_objects(self, request, uuids, xmin):
        errors = []
        for i, uuid in enumerate(uuids):
            error = self.update_object(request, uuid, xmin)
            if error is not None:
                errors.append(error)
            if (i + 1) % 50 == 0:
                LOG.info('Indexing %d', i + 1)
        return errors

    def update_object(self, request, uuid, xmin):  # pylint: disable=inconsistent-return-statements
        request.datastore = 'database'  # required by 2-step indexer
        last_exc = None
        try:
            doc = request.embed('/%s/@@index-data/' % uuid, as_user='INDEXER')
        except StatementError:
            # Can't reconnect until invalid transaction is rolled back
            raise
        except Exception as ecp: # pylint: disable=broad-except
            LOG.error('Error rendering /%s/@@index-data', uuid, exc_info=True)
            last_exc = repr(ecp)
        if last_exc is None:
            for backoff in [0, 10, 20, 40, 80]:
                time.sleep(backoff)
                try:
                    self.elastic_search.index(
                        index=doc['item_type'], doc_type=doc['item_type'], body=doc,
                        id=str(uuid), version=xmin, version_type='external_gte',
                        request_timeout=30,
                    )
                except StatementError:
                    # Can't reconnect until invalid transaction is rolled back
                    raise
                except ConflictError:
                    LOG.warning('Conflict indexing %s at version %d', uuid, xmin)
                    return
                except (ES_ConnectionError, ReadTimeoutError, TransportError) as ecp:
                    LOG.warning('Retryable error indexing %s: %r', uuid, ecp)
                    last_exc = repr(ecp)
                except Exception as ecp: # pylint: disable=broad-except
                    LOG.error('Error indexing %s', uuid, exc_info=True)
                    last_exc = repr(ecp)
                    break
                else:
                    # Get here on success and outside of try
                    return

        timestamp = datetime.datetime.now().isoformat()
        return {'error_message': last_exc, 'timestamp': timestamp, 'uuid': str(uuid)}

    def shutdown(self):
        pass
