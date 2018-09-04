from pyramid.view import view_config
from snovault.viewconfigs.report import ReportView
from snovault import AbstractCollection 
from snovault.resource_views import collection_view_listing_db
from snovault.viewconfigs.searchview import SearchView

def includeme(config):
    config.add_route('report', '/report{slash:/?}')
    config.scan(__name__)


@view_config(route_name='report', request_method='GET', permission='search')
def report(context, request):

    report = ReportView(context, request)
    return report.preprocess_view()