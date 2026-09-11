import re
import secrets
import string
import urllib.parse
from django.conf import settings

TOKEN_ALPHABET = string.ascii_letters + string.digits  # 62 alphanumeric characters


def generate_secure_token(length: int = 7) -> str:
    """
    Generates a cryptographically secure, non-predictable random alphanumeric token.
    Length between 6 and 12 chars (defaults to 7, yielding 62^7 = 3.52 trillion combinations).
    (Requirement 2 & 13)
    """
    length = max(6, min(12, length))
    return ''.join(secrets.choice(TOKEN_ALPHABET) for _ in range(length))


def validate_destination_url(url: str) -> bool:
    """
    Validates destination URLs before saving or redirecting.
    Only allows http:// and https:// URLs.
    Guards against open redirect vulnerabilities and CRLF injection.
    (Requirement 13)
    """
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    if '\r' in url or '\n' in url:
        return False

    try:
        parsed = urllib.parse.urlparse(url)
        return parsed.scheme.lower() in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        return False


def get_shortener_base_url(request=None) -> str:
    """
    Returns the branded short URL base domain.
    Defaults to https://marketing.iriscommunications.cloud
    """
    configured = getattr(settings, 'SHORTENER_BASE_URL', None)
    if not configured:
        configured = getattr(settings, 'BASE_TRACKING_URL', None)
    if not configured or configured in ('http://localhost:8000', 'http://127.0.0.1:8000'):
        # If in debug mode and a request is provided, we can use request host, but prefer branded domain
        if request and getattr(settings, 'USE_REQUEST_HOST_FOR_TRACKING', False):
            configured = f"{request.scheme}://{request.get_host()}"
        else:
            configured = 'https://marketing.iriscommunications.cloud'

    return configured.rstrip('/')


# Common bot / crawler regexes
BOT_USER_AGENTS = re.compile(
    r'(bot|crawler|spider|slurp|curl|wget|python-requests|urllib|headless|postman|'
    r'facebookexternalhit|twitterbot|linkedinbot|slackbot|whatsapp|telegrambot|'
    r'google-safety|adsbot-google|googlebot|bingbot|yahoo|yandex)',
    re.IGNORECASE
)

# Email security gateways & automated link scanners
SCANNER_USER_AGENTS = re.compile(
    r'(safelinks|bingpreview|msnbot|barracuda|proofpoint|mimecast|sophos|symantec|'
    r'fireeye|trendmicro|zscaler|kaspersky|avast|fortinet|forcepoint|cisco|'
    r'spamexperts|mailhostbox|ironport|messagelabs|appriver|retarus|hornetsecurity)',
    re.IGNORECASE
)


def detect_bot_and_device(request, recipient_link=None) -> dict:
    """
    Analyzes request headers, User-Agent, and client behavior to classify:
    - Click Type: HUMAN | SUSPECTED_BOT | UNKNOWN
    - Browser: Chrome, Edge, Safari, Firefox, Opera, Other
    - Operating System: Windows, macOS, Linux, iOS, Android, Other
    - Device Type: Desktop, Mobile, Tablet, Unknown
    (Requirement 14 & 16)
    """
    ua = request.META.get('HTTP_USER_AGENT', '').strip()
    referrer = request.META.get('HTTP_REFERER', '').strip() or None

    if not ua:
        click_type = 'UNKNOWN'
        detection_reason = 'Empty User-Agent'
    elif SCANNER_USER_AGENTS.search(ua):
        click_type = 'SUSPECTED_BOT'
        detection_reason = 'Matched email security scanner pattern'
    elif BOT_USER_AGENTS.search(ua):
        click_type = 'SUSPECTED_BOT'
        detection_reason = 'Matched crawler/bot signature'
    else:
        click_type = 'HUMAN'
        detection_reason = 'Standard browser signature'

    # Device Type detection
    ua_lower = ua.lower()
    if 'ipad' in ua_lower or ('android' in ua_lower and 'mobile' not in ua_lower) or 'tablet' in ua_lower:
        device_type = 'Tablet'
    elif 'mobi' in ua_lower or 'iphone' in ua_lower or 'ipod' in ua_lower or 'android' in ua_lower:
        device_type = 'Mobile'
    elif any(d in ua_lower for d in ['windows', 'macintosh', 'mac os', 'linux', 'x11', 'cros']):
        device_type = 'Desktop'
    else:
        device_type = 'Unknown'

    # Browser detection
    if 'edg/' in ua_lower or 'edge/' in ua_lower:
        browser = 'Edge'
    elif 'opr/' in ua_lower or 'opera' in ua_lower:
        browser = 'Opera'
    elif 'chrome' in ua_lower and 'chromium' not in ua_lower and 'edg' not in ua_lower:
        browser = 'Chrome'
    elif 'firefox' in ua_lower:
        browser = 'Firefox'
    elif 'safari' in ua_lower and 'chrome' not in ua_lower:
        browser = 'Safari'
    elif 'msie' in ua_lower or 'trident/' in ua_lower:
        browser = 'Internet Explorer'
    else:
        browser = 'Other'

    # OS detection
    if 'windows' in ua_lower:
        os_name = 'Windows'
    elif 'iphone' in ua_lower or 'ipad' in ua_lower or 'ios' in ua_lower:
        os_name = 'iOS'
    elif 'android' in ua_lower:
        os_name = 'Android'
    elif 'mac os' in ua_lower or 'macintosh' in ua_lower:
        os_name = 'macOS'
    elif 'linux' in ua_lower:
        os_name = 'Linux'
    else:
        os_name = 'Other'

    return {
        'click_type': click_type,
        'detection_reason': detection_reason,
        'browser': browser,
        'operating_system': os_name,
        'device_type': device_type,
        'user_agent': ua[:500],
        'referrer': referrer[:500] if referrer else None,
    }
