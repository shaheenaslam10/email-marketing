def tracking_base(request):
    """Exposes the canonical tracking base URL to every template.

    Single template-side source of truth: templates must use
    {{ shortener_base_url }} and never hard-code a domain.
    """
    from apps.tracking.utils import get_shortener_base_url
    return {'shortener_base_url': get_shortener_base_url()}
