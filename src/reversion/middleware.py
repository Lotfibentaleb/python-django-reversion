"""Middleware used by Reversion."""

from reversion.revisions import revision_context_manager


REVISION_MIDDLEWARE_FLAG = "reversion.revision_middleware_active"


class RevisionMiddleware(object):
    
    """Wraps the entire request in a revision."""
    
    def process_request(self, request):
        """Starts a new revision."""
        request.META[REVISION_MIDDLEWARE_FLAG] = True
        revision_context_manager.start()
        if hasattr(request, "user") and request.user.is_authenticated():
            revision_context_manager.set_user(request.user)
        
    def process_response(self, request, response):
        """Closes the revision."""
        if request.META.get(REVISION_MIDDLEWARE_FLAG, False):
            del request.META[REVISION_MIDDLEWARE_FLAG]
            revision_context_manager.end()
        return response
        
    def process_exception(self, request, exception):
        """Closes the revision."""
        revision_context_manager.invalidate()    