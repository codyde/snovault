from elasticsearch.exceptions import (
    ConflictError,
    ConnectionError,
    NotFoundError,
    TransportError,
)
from pyramid.view import view_config
from sqlalchemy.exc import StatementError
from snovault import (
    COLLECTIONS,
    DBSESSION,
)
from snovault.storage import (
    TransactionRecord,
)
from urllib3.exceptions import ReadTimeoutError
from .interfaces import (
    ELASTIC_SEARCH,
    INDEXER
)
from snovault import CONNECTION
import datetime
import logging
import pytz
import time
import copy
import json
import requests

es_logger = logging.getLogger("elasticsearch")
es_logger.setLevel(logging.ERROR)
log = logging.getLogger(__name__)
SEARCH_MAX = 99999  # OutOfMemoryError if too high

def includeme(config):
    config.add_route('index', '/index')
    config.add_route('_indexer_state', '/_indexer_state')
    config.scan(__name__)
    registry = config.registry
    registry[INDEXER] = Indexer(registry)

class IndexerState(object):
    # Keeps track of uuids and indexer state by cycle.  Also handles handoff of uuids to followup indexer
    def __init__(self, es, key, title='primary', followups=[]):
        self.es = es
        self.key = key  # "indexerstate"
        self.title           = title
        self.state_id        = self.title + '_indexer'       # State of the current or last cycle
        self.todo_set        = self.title + '_in_progress'   # one cycle of uuids, sent to the Secondary Indexer
        #self.failed_set      = self.title + '_failed'
        #self.done_set        = self.title + '_done'          # Trying to get all uuids from 'todo' to this set
        self.troubled_set    = self.title + '_troubled'      # uuids that failed to index in any cycle
        self.last_set        = self.title + '_last_cycle'    # uuids in the most recent finished cycle
        self.success_set     = None                          # None is the same as self.done_set
        self.cleanup_this_cycle = [self.todo_set]  # ,self.failed_set,self.done_set]  # Clean up at end of current cycle
        self.cleanup_last_cycle = [self.last_set,self.troubled_set]              # Clean up at beginning of next cycle
        self.override           = 'reindex_' + self.title      # If exists then reindex all
        # DO NOT INHERIT! These keys are for passing on to other indexers
        self.followup_prep_list      = 'primary_followup_prep_list' # Setting up the uuids to be handled by a followup process
        self.staged_for_vis_list     = 'staged_for_vis_indexer' # Followup list is added to here to pass baton
        self.staged_for_regions_list = 'staged_for_region_indexer'     # Followup list is added to here to pass baton
        self.followup_lists = []                                     # filled dynamically
        self.indexers = ['primary']
        for name in followups:
            if name != '':
                list_id = 'staged_for_' + name
                assert list_id == self.staged_for_vis_list or list_id == self.staged_for_regions_list
                self.followup_lists.append(list_id)
                self.indexers.append(name)
        self.clock = {}
        # some goals:
        # 1) Detect and recover from interrupted cycle - working but ignored for now
        # 2) Record (double?) failures and consider blacklisting them - not tried, could do.
        # 3) Make 2-pass indexing work

    # Private-ish primitives...
    def get_obj(self, id, type='meta'):
        try:
            return self.es.get(index=self.key, doc_type=type, id=id).get('_source',{})  # TODO: snovault/meta
        except:
            return {}

    def put_obj(self, id, obj, type='meta'):
        try:
            self.es.index(index=self.key, doc_type=type, id=id, body=obj)
        except:
            log.warn("Failed to save to es: " + id, exc_info=True)

    def delete_objs(self, ids, type='meta'):
        for id in ids:
            try:
                self.es.delete(index=self.key, doc_type=type, id=id)
            except:
                pass

    def get_list(self, id):
        return self.get_obj(id).get('list',[])

    def get_count(self, id):
        return self.get_obj(id).get('count',0)

    def put_list(self, id, a_list):
        return self.put_obj(id, { 'list': a_list, 'count': len(a_list) })

    #def get_diff(self,orig_id, subtract_ids):
    #    result_set = set(self.get_list(orig_id))
    #
    #    if len(result_set) > 0:
    #        for id in subtract_ids:
    #            subtract_list = self.get_list(id)
    #            if len(subtract_list):
    #                result_set = result_set.difference(set(subtract_list))
    #    return result_set

    #def set_add(self, id, vals):
    #    set_to_update = set(self.get_list(id))
    #    if len(set_to_update) > 0:
    #        set_to_update.update(vals)
    #    else:
    #        set_to_update = set(vals)
    #    self.put_list(id, set_to_update)

    def list_extend(self, id, vals):
        list_to_extend = self.get_list(id)
        if len(list_to_extend) > 0:
            list_to_extend.extend(vals)  # TODO: consider capping at SEARCH_MAX (keeping count but not uuids).  Requires followup handoff work.
        else:
            list_to_extend = vals

        self.put_list(id, list_to_extend)

    def rename_objs(self, from_id, to_id):
        val = self.get_list(from_id)
        if val:
            self.put_list(to_id, val)
            self.delete_objs([from_id])

    # Public access...
    def get(self):
        '''Returns the basic state info'''
        return self.get_obj(self.state_id)

    def put(self, state):
        '''Update the basic state info'''
        # Don't save errors in es
        errors = state.pop('errors', None)

        state['title'] = self.state_id
        self.put_obj(self.state_id, state)

        if errors is not None:
            state['errors'] = errors

    def request_reindex(self,requested):
        '''Requests full reindexing on next cycle'''
        if requested == 'all':
            self.put_obj(self.override, {self.title : 'reindex', 'all': True})
            if self.title == 'primary':  # If primary indexer delete the original master obj
                self.delete_objs(["indexing"])  # http://localhost:9200/snovault/meta/indexing

        else:
            uuids = requested.split(',')
            override_obj = self.get_obj(self.override)
            if 'uuids' not in override_obj:
                override_obj['uuids'] = uuids
            else:
                override_obj['uuids'].extend(uuids)
            self.put_obj(self.override, override_obj)

    def all_indexable_uuids(self, request):
        '''returns list of uuids pertinant to this indexer.'''
        return list(all_uuids(request.registry))

    def reindex_requested(self, request):
        '''returns list of uuids if a reindex was requested.'''
        override = self.get_obj(self.override)
        if override:
            if override.get('all', False):
                self.delete_objs([self.override] + self.followup_lists)
                return self.all_indexable_uuids(request)
            else:
                uuids =  override.get('uuids',[])
                uuid_count = len(uuids)
                if uuid_count > 0:
                    if uuid_count > SEARCH_MAX:
                        self.delete_objs([self.override] + self.followup_lists)
                    else:
                        self.delete_objs([self.override])
                    return uuids
        return None


    def get_initial_state(self):
        '''Useful to initialize at idle cycle'''
        new_state = { 'title': self.state_id, 'status': 'idle'}
        state = self.get()
        for var in ['cycles']:  # could expand this list
            val = state.pop(var,None)
            if val is not None:
                new_state[var] = val
        return new_state

    def start_clock(self, name):
        '''Can start a named clock and use it later to figure out elapsed time'''
        self.clock[name] = datetime.datetime.now(pytz.utc)

    def elapsed(self, name):
        '''Returns string of time elapsed since named clock started.'''
        start = self.clock.get(name)
        if start is None:
            return 'unknown'
        else:
            return str(datetime.datetime.now(pytz.utc) - start)

    def priority_cycle(self, request):
        '''Initial startup, reindex, or interupted prior cycle can all lead to a priority cycle.
           returns (discovered xmin, uuids, whether previous cycle was interupted).'''
        # Not yet started?
        initialized = self.get_obj("indexing")  # http://localhost:9200/snovault/meta/indexing
        if not initialized:
            self.delete_objs([self.override] + self.followup_lists)
            state = self.get()
            state['status'] = 'uninitialized'
            self.put(state)
            return (-1, [], False)  # primary indexer will know what to do and secondary indexer should do nothing yet

        state = self.get()

        # Rare call for reindexing...
        reindex_uuids = self.reindex_requested(request)
        if reindex_uuids is not None and reindex_uuids != []:
            uuids_count = len(reindex_uuids)
            log.warn('%s reindex of %d uuids requested' % (self.state_id, uuids_count))
            return (-1, reindex_uuids, False)

        if state.get('status', '') != 'indexing':
            return (-1, [], False)

        xmin = state.get('xmin', -1)
        #snapshot = state.get('snapshot', None)
        if xmin == -1:  # or snapshot is None:
            return (-1, [], False)

        #assert(self.get_count(self.done_set) == 0)  # Valid for cycle-level accounting only
        #undone_uuids = self.get_diff(self.todo_set, [self.done_set])  # works for any accountingu
        undone_uuids = self.get_list(self.todo_set)                    # works fastest for cycle-level accounting
        if len(undone_uuids) <= 0:  # TODO SEARCH_MAX?  SEARCH_MAX/10
            return (-1, [], False)

        # Note: do not clean up last cycle yet because we could be restarted multiple times.
        return (xmin, undone_uuids, True)


    def prep_for_followup(self, xmin, uuids):
        '''Prepare a cycle of uuids for passing to a followup indexer (e.g. audits, viscache)'''
        prep_list = [ "xmin:%s" % xmin ]
        prep_list.extend(uuids)
        self.put_list(self.followup_prep_list, prep_list)
        # No need to preserve anything on the prep_list as it passes to the staged list in one cycle.

    def start_cycle(self, uuids, state=None):
        '''Every indexing cycle must be properly opened.'''
        self.clock = {}
        self.start_clock('cycle')
        if state is None:
            state = self.get()
        state['cycle_started'] = datetime.datetime.now().isoformat()
        state['status'] = 'indexing'
        state['cycle_count'] = len(uuids)

        self.put(state)
        self.delete_objs(self.cleanup_last_cycle)
        self.delete_objs(self.cleanup_this_cycle)
        self.put_list(self.todo_set, set(uuids))
        return state

    def start_pass2(self, state):
        state['pass1_took'] = self.elapsed('cycle')
        self.put(state)
        self.start_clock('pass2')
        return state


    def add_errors(self, errors, finished=True):
        '''To avoid 16 worker concurency issues, errors are recorded at the end of a cycle.'''
        uuids = [err['uuid'] for err in errors]  # better be uuids!
        if len(uuids) > 0:
            # Forget extend, this is cycle-level accounting
            #self.list_extend(self.failed_set, uuids)
            # Forget failed_set, this is cycle-level accounting
            #self.put_list(self, self.failed_set, uuids)

            if finished:
                # Forget sets... cycle-level accounting so errors => failed_set => troubled_set all in one cycle.
                # handle troubled uuids:
                #troubled_uuids = set(self.get_list(self.failed_set))
                #if len(troubled_uuids):
                #    self.set_add(self.troubled_set, troubled_uuids)
                #    # TODO: could make doubled_troubled set and use it to blacklist uuids
                self.put_list(self.troubled_set, uuids)

    def finish_cycle(self, state, errors=None):
        '''Every indexing cycle must be properly closed.'''

        if 'pass2' in self.clock.keys():
            state['pass2_took'] = self.elapsed('pass2')

        if errors:  # By handling here, we avoid overhead and concurrency issues of uuid-level accounting
            self.add_errors(errors)

        # pass any staged items to followup
        if self.followup_prep_list is not None:
            # TODO: send signal for 'all' when appropriate.  Saves the following expensive lines.
            hand_off_list = self.get_list(self.followup_prep_list)
            if len(hand_off_list) > 0:  # Have to push because ready_list may still have previous cycles in it
                for id in self.followup_lists:
                    self.list_extend(id, hand_off_list)
                    #log.warn("prmary added to %s" % id)
                self.delete_objs([self.followup_prep_list])

        # cycle-level accounting so todo => done => last in this function
        #self.rename_objs(self.todo_set, self.done_set)
        done_count = self.get_count(self.todo_set)
        self.rename_objs(self.todo_set, self.last_set)

        if self.success_set is not None:
            state[self.title + '_updated'] = self.get_count(self.success_set)
        cycle_count = state.pop('cycle_count', None)
        #assert(cycle_count == done_count)
        state['indexed'] = done_count

        #self.rename_objs(self.done_set, self.last_set)   # cycle-level accounting so todo => done => last in this function
        self.delete_objs(self.cleanup_this_cycle)
        state['status'] = 'done'
        state['cycles'] = state.get('cycles', 0) + 1
        state['cycle_took'] = self.elapsed('cycle')

        self.put(state)

        return state

    def get_notice_user(self, user):
        '''Returns the user token for a named user.'''
        slack_users = self.get_obj('slack_users',{})
        if not slack_users:
            try:
                r = requests.get('https://slack.com/api/users.list?token=xoxb-197478914097-Tf1hh7JDQnU9ZL34lpBawn0T')
                resp = json.loads(r.text)
                if not resp['ok']:
                    log.warn(resp)
                    return None
                members = resp.get('members',[])
                for member in members:
                    slack_users[member['name']] = member['id']
                self.put_obj('slack_users',slack_users)
            except:
                return None
        return slack_users.get(user)

    def set_notices(self, from_host, who=None, bot_token=None, which=None):
        '''Set up notification so that slack bot can send a message when indexer finishes'''
        if who is None and bot_token is None:
            return "ERROR: must specify who to notify or bot_token"
        if which is None:
            which = self.title
        if which not in ['primary','vis','region','all']:  # WARNING: hard-coded list
            return "ERROR: unknown indexer to monitor: %s" % (which)

        notify = self.get_obj('notify', 'default')
        if bot_token is not None:
            notify['bot_token'] = bot_token

        if 'from' not in notify:
            notify['from'] = from_host

        user_warns = ''
        if who is not None:
            indexer_notices = notify.get(which,{})
            if who in ['none','noone','nobody','stop','']:
                notify.pop(which, None)
            else:
                if which == 'all':
                    if 'indexers' not in indexer_notices:
                        indexer_notices['indexers'] = ['primary','vis','region']  # WARNING: hard-coded list
                users = who.split(',')
                # TODO: verify users, convert users from names to tokens?
                for name in users:
                    if self.get_notice_user(name) is None:
                        user_warns += ', ' + name
                who_all = indexer_notices.get('who',[])
                who_all.extend(users)
                indexer_notices['who'] = list(set(who_all))
                notify[which] = indexer_notices

        self.put_obj('notify', notify, 'default')
        if user_warns != '':
            user_warns = 'Unknown users: ' + user_warns[2:]
        if 'bot_token' not in notify:
            return "WARNING: bot_token is required. " + user_warns
        elif user_warns != '':
            return "WARNING: " + user_warns
        else:
            return notify

    def get_notices(self, full=False):
        '''Get the notifications'''
        notify = self.get_obj('notify','default')
        if full:
            return notify
        notify.pop('bot_token', None)
        notify.pop('from', None)
        indexers = ['primary','vis','region','all']  # WARNING: hard-coded list
        if self.title == 'primary':
            for indexer in indexers:
                if notify.get(indexer,{}):
                    return notify  # return if anything
        else:
            indexers.remove(self.title)
            indexers.remove('all')
            for indexer in indexers:
                notify.pop(indexer,None)
            for indexer in [self.title, 'all']:
                if notify.get(indexer,{}):
                    return notify  # return if anything

        return {}

    def send_notices(self):
        '''Sends notifications when indexer is done.'''
        # https://slack.com/api/chat.postMessage?token=xoxb-197478914097-Tf1hh7JDQnU9ZL34lpBawn0T&channel=U1KPQK1HN&text=Yay!
        notify = self.get_obj('notify','default')
        if not notify:
            return
        if 'bot_token' not in notify or 'from' not in notify:
            return  # silent failure

        changed = False
        text = None
        who = []
        if 'all' in notify:
            # if all indexers are done, then report
            indexers = notify['all'].get('indexers',[])
            if self.title in indexers:
                indexers.remove(self.title)
            if len(indexers) > 0:
                notify['all']['indexers'] = indexers
            else:
                who.extend(notify['all'].get('who',[]))
                notify.pop('all',None)
                text='All indexers are done for %s' % (notify['from'])
            changed = True
        if self.title in notify:
            who.extend(notify[self.title].get('who',[]))
            notify.pop(self.title, None)
            changed = True
            if text is None:
                text='%s indexer is done for %s' % (self.title, notify['from'])
        if len(who) > 0 and text is not None:
            who = list(set(who))
            users = ''
            msg = ''
            if len(who) == 1:
                #channel='U1KPQK1HN'  # TODO: look up user token
                channel = self.get_notice_user(who[0])
                if channel:
                    msg = 'token=%s&channel=%s&text=%s' % (notify['bot_token'],channel,text)
                # otherwise fall back on generic message.
            if msg == '':
                channel='dcc-private'  # TODO: user OR DCC-private
                for user in who:
                    users += '@' + user + ', '
                msg = 'token=%s&channel=%s&link_names=true&text=%s%s' % (notify['bot_token'],channel,users,text)

            try:
                r = requests.get('https://slack.com/api/chat.postMessage?' + msg)
                resp = json.loads(r.text)
                if not resp['ok']:
                    log.warn(resp)
            except:
                pass   # silent failure
        if changed:
            self.put_obj('notify', notify, 'default')

    def display(self):
        display = {}
        display['state'] = self.get()
        display['title'] = display['state']['title']
        display['uuids in progress'] = self.get_count(self.todo_set)
        display['uuids troubled'] = self.get_count(self.troubled_set)
        display['uuids last cycle'] = self.get_count(self.last_set)
        if self.followup_prep_list is not None:
            display['to be handed off to other indexer(s)'] = self.get_count(self.followup_prep_list)
        if self.title == 'primary':
            for id in self.followup_lists:
                display[id] = self.get_count(id)
        else:
            id = 'staged_for_%s_list' % (self.title)
            display['staged by primary'] = self.get_count(id)

        reindex = self.get_obj(self.override)
        if reindex:
            uuids = reindex.get('uuids')
            if uuids is not None:
                display['REINDEX requested'] = uuids
            elif reindex.get('all',False):
                display['REINDEX requested'] = True
        notify = self.get_notices()
        if notify:
            display['NOTIFY requested'] = notify
        display['now'] = datetime.datetime.now().isoformat()

        return display


@view_config(route_name='_indexer_state', request_method='GET', permission="index")
def indexer_state_show(request):
    es = request.registry[ELASTIC_SEARCH]
    INDEX = request.registry.settings['snovault.elasticsearch.index']
    state = IndexerState(es,INDEX)

    # requesting reindex
    reindex = request.params.get("reindex")
    if reindex is not None:
        state.request_reindex(reindex)

    # Requested notification
    who = request.params.get("notify")
    bot_token = request.params.get("bot_token")
    if who is not None or bot_token is not None:
        notices = state.set_notices(request.host_url, who, bot_token, request.params.get("indexers"))
        if isinstance(notices,str):
            return notices

    display = state.display()
    try:
        count = es.count(index=INDEX).get('count',0)  # TODO: 'vis_composite' should be obtained from visualization.py
        if count:
            display['docs in index'] = count
    except:
        display['docs in index'] = 'Not Found'
        pass

    if not request.registry.settings.get('testing',False):  # NOTE: _indexer not working on local instances
        try:
            r = requests.get(request.host_url + '/_indexer')
            display['listener'] = json.loads(r.text)
            display['status'] = display['listener']['status']
        except:
            log.error('Error getting /_indexer', exc_info=True)

    # always return raw json
    #if len(request.query_string) > 0:
    #    request.query_string = "&format=json"
    #else:
    request.query_string = "format=json"
    return display


@view_config(route_name='index', request_method='POST', permission="index")
def index(request):
    INDEX = request.registry.settings['snovault.elasticsearch.index']
    # Setting request.datastore here only works because routed views are not traversed.
    request.datastore = 'database'
    record = request.json.get('record', False)
    dry_run = request.json.get('dry_run', False)
    recovery = request.json.get('recovery', False)
    es = request.registry[ELASTIC_SEARCH]
    indexer = request.registry[INDEXER]
    session = request.registry[DBSESSION]()
    connection = session.connection()
    first_txn = None
    snapshot_id = None
    restart=False
    invalidated = []
    xmin = -1

    # Currently 2 possible followup indexers (production.ini.inL [set stage_for_followup = "vis_indexer region_indexer"])
    stage_for_followup = list(request.registry.settings.get("stage_for_followup", '').split(', '))
    use_2pass = False # request.registry.settings.get("index_with_2pass", False)  # defined in base.ini for encoded

    # May have undone uuids from prior cycle
    state = IndexerState(es, INDEX, followups=stage_for_followup)

    (xmin, invalidated, restart) = state.priority_cycle(request)
    # OPTIONAL: restart support
    if restart:  # Currently not bothering with restart!!!
        xmin = -1
        invalidated = []
    # OPTIONAL: restart support

    result = state.get_initial_state()  # get after checking priority!
    if len(invalidated) > 0:
        if xmin == -1:
            xmin = get_current_xmin(request)
        flush = True
    else:
        xmin = get_current_xmin(request)

        last_xmin = None
        if 'last_xmin' in request.json:
            last_xmin = request.json['last_xmin']
        else:
            try:
                status = es.get(index=INDEX, doc_type='meta', id='indexing')
            except NotFoundError:
                interval_settings = {"index": {"refresh_interval": "30s"}}
                es.indices.put_settings(index=INDEX, body=interval_settings)
                pass
            else:
                last_xmin = status['_source'].get('xmin')

        result.update(
            xmin=xmin,
            last_xmin=last_xmin,
        )

        flush = False
        if last_xmin is None:
            result['types'] = types = request.json.get('types', None)
            invalidated = list(all_uuids(request.registry, types))
            flush = True
        else:
            txns = session.query(TransactionRecord).filter(
                TransactionRecord.xid >= last_xmin,
            )

            invalidated = set()
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

            result['txn_count'] = txn_count
            if txn_count == 0:
                return result

            es.indices.refresh(index=INDEX)
            res = es.search(index=INDEX, size=SEARCH_MAX, body={
                'filter': {
                    'or': [
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
                '_source': False,
            })
            if res['hits']['total'] > SEARCH_MAX:
                invalidated = list(all_uuids(request.registry))
                flush = True
            else:
                referencing = {hit['_id'] for hit in res['hits']['hits']}
                invalidated = referencing | updated
                result.update(
                    max_xid=max_xid,
                    renamed=renamed,
                    updated=updated,
                    referencing=len(referencing),
                    invalidated=len(invalidated),
                    txn_count=txn_count,
                    first_txn_timestamp=first_txn.isoformat(),
                )

            if invalidated and not dry_run:
                # Exporting a snapshot mints a new xid, so only do so when required.
                # Not yet possible to export a snapshot on a standby server:
                # http://www.postgresql.org/message-id/CAHGQGwEtJCeHUB6KzaiJ6ndvx6EFsidTGnuLwJ1itwVH0EJTOA@mail.gmail.com
                if snapshot_id is None and not recovery:
                    snapshot_id = connection.execute('SELECT pg_export_snapshot();').scalar()

    if invalidated and not dry_run:
        if len(stage_for_followup) > 0:
            # Note: undones should be added before, because those uuids will (hopefully) be indexed in this cycle
            state.prep_for_followup(xmin, invalidated)

        result = state.start_cycle(invalidated, result)

        # Do the work...

        if use_2pass:
            log.info("indexer starting pass 1 on %d uuids", len(invalidated))
        errors = indexer.update_objects(request, invalidated, xmin, snapshot_id, restart)

        if use_2pass:
            result = state.start_pass2(result)
            log.info("indexer starting pass 2 on %d uuids", len(invalidated))
            audit_errors = indexer.update_audits(request, invalidated, xmin, snapshot_id)  # ignore restart
            if len(audit_errors):
                errors.extend(audit_errors)

        result = state.finish_cycle(result,errors)

        if errors:
            result['errors'] = errors

        if record:
            try:
                es.index(index=INDEX, doc_type='meta', body=result, id='indexing')
            except:
                error_messages = copy.deepcopy(result['errors'])
                del result['errors']
                es.index(index=INDEX, doc_type='meta', body=result, id='indexing')
                for item in error_messages:
                    if 'error_message' in item:
                        log.error('Indexing error for {}, error message: {}'.format(item['uuid'], item['error_message']))
                        item['error_message'] = "Error occured during indexing, check the logs"
                result['errors'] = error_messages

            if es.indices.get_settings(index=INDEX)[INDEX]['settings']['index'].get('refresh_interval', '') != '1s':
                interval_settings = {"index": {"refresh_interval": "1s"}}
                es.indices.put_settings(index=INDEX, body=interval_settings)

        es.indices.refresh(index=INDEX)
        if flush:
            try:
                es.indices.flush_synced(index=INDEX)  # Faster recovery on ES restart
            except ConflictError:
                pass

    if first_txn is not None:
        result['txn_lag'] = str(datetime.datetime.now(pytz.utc) - first_txn)

    state.send_notices()
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

def all_uuids(registry, types=None):
    # First index user and access_key so people can log in
    collections = registry[COLLECTIONS]
    initial = ['user', 'access_key']
    for collection_name in initial:
        collection = collections.by_item_type.get(collection_name, [])
        # for snovault test application, there are no users or keys
        if types is not None and collection_name not in types:
            continue
        for uuid in collection:
            yield str(uuid)
    for collection_name in sorted(collections.by_item_type):
        if collection_name in initial:
            continue
        if types is not None and collection_name not in types:
            continue
        collection = collections.by_item_type[collection_name]
        for uuid in collection:
            yield str(uuid)


class Indexer(object):
    def __init__(self, registry):
        self.es = registry[ELASTIC_SEARCH]
        self.index = registry.settings['snovault.elasticsearch.index']
        self.use_2pass = False #registry.settings.get("index_with_2pass", False)

    def update_objects(self, request, uuids, xmin, snapshot_id=None, restart=False):
        errors = []
        for i, uuid in enumerate(uuids):
            error = self.update_object(request, uuid, xmin)
            if error is not None:
                errors.append(error)
            if (i + 1) % 50 == 0:
                log.info('Indexing %d', i + 1)

        return errors

    def update_object(self, request, uuid, xmin, restart=False):
        request.datastore = 'database'  # required by 2-step indexer

        # OPTIONAL: restart support
        # If a restart occurred in the middle of indexing, this uuid might have already been indexd, so skip redoing it.
        # if restart:
        #     try:
        #         #if self.es.exists(index=self.index, id=str(uuid), version=xmin, version_type='external_gte'):  # couldn't get exists to work.
        #         result = self.es.get(index=self.index, id=str(uuid), _source_include='uuid', version=xmin, version_type='external_gte')
        #         if result.get('_source') is not None:
        #             return
        #     except:
        #         pass
        # OPTIONAL: restart support

        last_exc = None
        try:
            if self.use_2pass:
                doc = request.embed('/%s/@@index-data/noaudit/' % uuid, as_user='INDEXER')
            else:
                doc = request.embed('/%s/@@index-data/' % uuid, as_user='INDEXER')
        except StatementError:
            # Can't reconnect until invalid transaction is rolled back
            raise
        except Exception as e:
            log.error('Error rendering /%s/@@index-data', uuid, exc_info=True)
            last_exc = repr(e)

        if last_exc is None:
            if self.use_2pass:
                try:
                    audit = self.es.get(index=self.index, id=str(uuid)).get('_source',{}).get('audit')  # Any version
                    if audit:
                        doc.update(
                            audit=audit,
                            audit_stale=True,
                        )
                except:
                    pass

            for backoff in [0, 10, 20, 40, 80]:
                time.sleep(backoff)
                try:
                    self.es.index(
                        index=self.index, doc_type=doc['item_type'], body=doc,
                        id=str(uuid), version=xmin, version_type='external_gte',
                        request_timeout=30,
                    )
                except StatementError:
                    # Can't reconnect until invalid transaction is rolled back
                    raise
                except ConflictError:
                    log.warning('Conflict indexing %s at version %d', uuid, xmin)
                    return
                except (ConnectionError, ReadTimeoutError, TransportError) as e:
                    log.warning('Retryable error indexing %s: %r', uuid, e)
                    last_exc = repr(e)
                except Exception as e:
                    log.error('Error indexing %s', uuid, exc_info=True)
                    last_exc = repr(e)
                    break
                else:
                    # Get here on success and outside of try
                    return

        timestamp = datetime.datetime.now().isoformat()
        return {'error_message': last_exc, 'timestamp': timestamp, 'uuid': str(uuid)}

    def update_audits(self, request, uuids, xmin, snapshot_id=None):
        # Only called when use_2pass is True
        errors = []
        for i, uuid in enumerate(uuids):
            error = self.update_audit(request, uuid, xmin)
            if error is not None:
                errors.append(error)
            if (i + 1) % 50 == 0:
                log.info('Auditing %d', i + 1)

        return errors

    def update_audit(self, request, uuid, xmin):
        # Only called when use_2pass is True
        request.datastore = 'elasticsearch'  # Audits are built on elastic search !!!

        last_exc = None
        # First get the object currently in es
        try:
            result = self.es.get(index=self.index, id=str(uuid), version=xmin, version_type='external_gte')
            doc = result['_source']
        except StatementError:
            # Can't reconnect until invalid transaction is rolled back
            raise
        except Exception as e:
            log.error("Error can't find %s in %s", uuid, ELASTIC_SEARCH)
            last_exc = repr(e)

        ### # Handle audits:
        index_audit = False
        if last_exc is None:
            # It might be possible to assert that the audit is either empty or stale
            # TODO assert('audit_stale' is in doc or doc.get('audit') is None)

            try:
                # using audit-now bypasses cached_views 'audit' ensureing the audit is recacluated.
                audit = request.embed(str(uuid), '@@audit-now', as_user='INDEXER')['audit']
                if audit or doc.get('audit_stale',False):
                    doc.update(
                        audit=audit,
                        audit_stale=False,
                    )
                    index_audit = True
            except StatementError:
                # Can't reconnect until invalid transaction is rolled back
                raise
            except Exception as e:
                log.error('Error rendering /%s/@@index-audits', uuid, exc_info=True)
                last_exc = repr(e)

        if index_audit:
            if last_exc is None:
                for backoff in [0, 10, 20, 40, 80]:
                    time.sleep(backoff)
                    try:
                        self.es.index(
                            index=self.index, doc_type=doc['item_type'], body=doc,
                            id=str(uuid), version=xmin, version_type='external_gte',
                            request_timeout=30,
                        )
                    except StatementError:
                        # Can't reconnect until invalid transaction is rolled back
                        raise
                    except ConflictError:
                        #log.warning('Conflict indexing %s at version %d', uuid, xmin)
                        # This case occurs when the primary indexer is cycles ahead of the secondary indexer
                        # And this uuid will be secondarily indexed again on a second round
                        # So no error, pretend it has indexed and move on.
                        return  # No use doing any further secondary indexing
                    except (ConnectionError, ReadTimeoutError, TransportError) as e:
                        log.warning('Retryable error indexing %s: %r', uuid, e)
                        last_exc = repr(e)
                    except Exception as e:
                        log.error('Error indexing %s', uuid, exc_info=True)
                        last_exc = repr(e)
                        break
                    else:
                        # Get here on success and outside of try
                        return

        if last_exc is not None:
            timestamp = datetime.datetime.now().isoformat()
            return {'error_message': last_exc, 'timestamp': timestamp, 'uuid': str(uuid)}

    def shutdown(self):
        pass
