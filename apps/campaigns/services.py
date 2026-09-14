import html as html_module
import re
import urllib.parse
from typing import Dict, Any, Tuple, List
from django.conf import settings
from apps.contacts.models import Contact


def render_content_variables(
    template_str: str,
    contact: Contact,
    extra_context: Dict[str, Any] = None
) -> str:
    """
    Interpolates personalization variables in subject, html_content, and text_content.
    Supports standard contact fields, encrypted login password, and custom fields.
    (Section 14, 34, 35)
    """
    if not template_str:
        return ""

    context = {
        'first_name': contact.first_name or (contact.name.split(' ')[0] if contact.name else ''),
        'last_name': contact.last_name or '',
        'name': contact.name or '',
        'email': contact.email or '',
        'phone': contact.phone_number or '',
        'phone_number': contact.phone_number or '',
        'login': contact.phone_number or contact.email or '',
        'job_id': contact.job_id or '',
        'status': contact.status or '',
        # Decrypt password in memory only for interpolation
        'login_password': contact.login_password or '',
        'password': contact.login_password or '',
    }

    # Fetch global custom field values
    for cv in contact.custom_values.select_related('field').all():
        context[cv.field.slug.lower()] = cv.value or ''

    # Fetch group-specific custom field values (e.g. url, label, submission_uuid, used_at)
    if hasattr(contact, 'group_custom_values'):
        for gv in contact.group_custom_values.select_related('field').all():
            slug = gv.field.slug.lower()
            val = (gv.value or '').strip()
            # If contact value is empty, fallback to field description if it is a URL or default
            if not val and gv.field.description:
                desc = gv.field.description.strip()
                if desc.startswith(('http://', 'https://')) or gv.field.field_type == 'URL':
                    val = desc
            if val:
                context[slug] = val
                if slug in ('url', 'survey_url') or gv.field.field_type == 'URL':
                    context['url'] = val
                    context['survey_url'] = val
            elif slug not in context:
                context[slug] = ''

    # Fallback to group custom field definitions on any group the contact belongs to
    for grp in contact.groups.prefetch_related('custom_fields').all():
        for field in grp.custom_fields.filter(is_active=True):
            f_slug = field.slug.lower()
            if not context.get(f_slug):
                desc = (field.description or '').strip()
                if desc.startswith(('http://', 'https://')) or field.field_type == 'URL':
                    context[f_slug] = desc
                    if f_slug in ('url', 'survey_url') or field.field_type == 'URL':
                        context['url'] = desc
                        context['survey_url'] = desc

    # Check JSON custom_fields dictionary if stored directly on contact
    if hasattr(contact, 'custom_fields') and isinstance(contact.custom_fields, dict):
        for k, v in contact.custom_fields.items():
            if v:
                context[k.lower()] = str(v)

    # Fallback cross-mapping for url & survey_url
    if not context.get('url') and context.get('survey_url'):
        context['url'] = context['survey_url']
    if not context.get('survey_url') and context.get('url'):
        context['survey_url'] = context['url']

    if extra_context:
        context.update(extra_context)

    # Perform case-insensitive regex substitution for {{variable}} or {{ variable }}
    def replace_var(match):
        var_name = match.group(1).strip().lower()
        return str(context.get(var_name, ""))

    rendered = re.sub(r'\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}', replace_var, template_str)
    return rendered


def wrap_tracking(
    html_content: str,
    token_str: str,
    track_opens: bool = True,
    track_clicks: bool = True,
    campaign=None,
    contact=None
) -> str:
    """
    Injects open tracking pixel and rewrites links for recipient-level short URL click tracking.
    Generates unique branded tracking links per Campaign + Contact + URL.
    (Requirements 1, 2, 3, 7, 10, 15)
    """
    base_url = getattr(settings, 'BASE_TRACKING_URL', 'http://localhost:8000').rstrip('/')

    from apps.tracking.utils import generate_secure_token, validate_destination_url, build_campaign_short_url

    # Rewrite unsubscribe tag
    unsub_url = f"{base_url}/t/unsubscribe/{token_str}/"
    html_content = html_content.replace('{{unsubscribe_url}}', unsub_url)

    # Click tracking / placeholder resolution.
    # NOTE: this block always runs (not only when track_clicks is True) so a
    # {unique_link} placeholder can never ship literally: when tracking is
    # disabled at link level (data-track="false") or campaign level
    # (track_clicks=False), tracked anchors resolve to their direct
    # destination instead of a short URL. Anchors with explicit
    # data-track="true" (Step 5 Track URL = ON) resolve to the short URL
    # as plain text with no <a> element, so providers cannot rewrite it.
    if True:
        from apps.tracking.models import ShortenedLink, CampaignTrackingLink

        def replace_anchor(match):
            tag_attrs = match.group(1)
            inner_html = match.group(2)

            # Extract href (unescape entities: editor serialization stores
            # multi-param URLs with &amp; which must become & again)
            href_m = re.search(r'href=["\']([^"\']+)["\']', tag_attrs, re.IGNORECASE)
            if not href_m:
                return match.group(0)
            raw_href = html_module.unescape(href_m.group(1)).strip()

            # Ignore internal or non-http links
            if raw_href.startswith(('mailto:', 'tel:', '#', 'javascript:')) or '/t/unsubscribe/' in raw_href:
                return match.group(0)

            # Check for data-original-url (also entity-unescaped)
            orig_m = re.search(r'data-original-url=["\']([^"\']+)["\']', tag_attrs, re.IGNORECASE)
            orig_url = html_module.unescape(orig_m.group(1)).strip() if orig_m else ''

            # Explicit per-link opt-out?
            track_m = re.search(r'data-track=["\'](true|false)["\']', tag_attrs, re.IGNORECASE)
            tracking_disabled = bool(track_m and track_m.group(1).lower() == 'false')
            # Explicit per-link opt-in (Step 5 Insert URL with Track URL = ON):
            # first-party IRIS tracking. These links are emitted as plain
            # text (no <a> element) so delivery providers receive body text
            # they cannot link-rewrite; recipient mail apps auto-link the
            # bare URL and clicks still reach our /c/<token> endpoint.
            tracking_explicit = bool(track_m and track_m.group(1).lower() == 'true')

            # Resolve the {unique_link} placeholder to the real destination.
            # Only a bare placeholder with no data-original-url (legacy content
            # saved before the editor preserved metadata) is left untouched.
            has_placeholder = '{unique_link}' in raw_href or '{unique-link}' in raw_href
            if has_placeholder:
                if orig_url and '{unique_link}' not in orig_url and '{unique-link}' not in orig_url:
                    target_url = orig_url
                else:
                    return match.group(0)
            else:
                target_url = orig_url or raw_href

            # Validate target URL
            if not validate_destination_url(target_url):
                return match.group(0)

            # Tracking disabled (link-level or campaign-level): emit the
            # direct destination URL, never a tracking endpoint.
            if tracking_disabled or not track_clicks:
                if not has_placeholder:
                    return match.group(0)
                direct_attrs = re.sub(
                    r'href=["\'][^"\']+["\']', f'href="{target_url}"',
                    tag_attrs, count=1, flags=re.IGNORECASE,
                )
                direct_inner = inner_html.replace('{unique_link}', target_url).replace('{unique-link}', target_url)
                return f'<a {direct_attrs}>{direct_inner}</a>'

            # Extract link name/label
            name_m = re.search(r'data-link-name=["\']([^"\']+)["\']', tag_attrs, re.IGNORECASE)
            if name_m:
                link_name = html_module.unescape(name_m.group(1)).strip()
            else:
                plain_txt = html_module.unescape(re.sub(r'<[^>]+>', '', inner_html)).strip()
                link_name = plain_txt[:60] if plain_txt and not plain_txt.startswith(('http://', 'https://', '{unique')) else ''

            final_url = None
            if campaign and contact:
                try:
                    shortened_link, _ = ShortenedLink.objects.get_or_create(
                        campaign=campaign,
                        original_url=target_url,
                        defaults={
                            'link_name': link_name or 'Tracked Link',
                            'tracking_enabled': True
                        }
                    )
                    if link_name and (not shortened_link.link_name or shortened_link.link_name == 'Tracked Link'):
                        shortened_link.link_name = link_name
                        shortened_link.save(update_fields=['link_name'])

                    # Branded recipient URL on the unified /c/<token> endpoint.
                    # One opaque token per (campaign, contact, destination);
                    # reuses the existing shortener base-URL config.
                    recipient_link = CampaignTrackingLink.objects.filter(
                        shortened_link=shortened_link,
                        campaign=campaign,
                        contact=contact,
                        link_type=CampaignTrackingLink.LinkType.RECIPIENT,
                    ).first()

                    if not recipient_link:
                        # Generate unique token
                        for _ in range(10):
                            new_token = generate_secure_token(8)
                            if not CampaignTrackingLink.objects.filter(tracking_token=new_token).exists():
                                break
                        rec_short_url = build_campaign_short_url(new_token)
                        recipient_link = CampaignTrackingLink.objects.create(
                            campaign=campaign,
                            link_type=CampaignTrackingLink.LinkType.RECIPIENT,
                            name=link_name or 'Tracked Link',
                            shortened_link=shortened_link,
                            destination_url=target_url,
                            contact=contact,
                            tracking_token=new_token,
                            short_url=rec_short_url
                        )

                    final_url = recipient_link.short_url
                except Exception as e:
                    # Fallback to legacy tracking if database error
                    encoded_url = urllib.parse.quote(target_url, safe='')
                    final_url = f"{base_url}/t/click/{token_str}/?url={encoded_url}"
            else:
                # Standalone preview or no contact
                encoded_url = urllib.parse.quote(target_url, safe='')
                final_url = f"{base_url}/t/click/{token_str}/?url={encoded_url}"

            # First-party tracked links (Insert URL, Track URL = ON) render
            # as plain text with no anchor element (see tracking_explicit).
            if tracking_explicit:
                return html_module.escape(final_url)

            # Replace href attribute
            new_tag_attrs = re.sub(r'href=["\'][^"\']+["\']', f'href="{final_url}"', tag_attrs, count=1, flags=re.IGNORECASE)
            new_inner = inner_html.replace('{unique_link}', final_url).replace('{unique-link}', final_url)
            return f'<a {new_tag_attrs}>{new_inner}</a>'

        html_content = re.sub(r'<a\s+([^>]+)>([\s\S]*?)</a>', replace_anchor, html_content, flags=re.IGNORECASE)

    # Open tracking pixel (Section 75, Requirement 15)
    if track_opens:
        pixel_url = f"{base_url}/t/open/{token_str}/"
        pixel_tag = f'<img src="{pixel_url}" width="1" height="1" border="0" alt="" style="display:none !important;" />'
        if '</body>' in html_content:
            html_content = html_content.replace('</body>', f'{pixel_tag}</body>')
        else:
            html_content += pixel_tag

    return html_content


def validate_campaign_variables(html_content: str, subject: str, contacts: List[Contact]) -> Dict[str, Any]:
    """
    Validates variable presence across selected contacts (Section 36).
    Checks missing login password, missing JobID, missing first name.
    """
    combined_text = f"{subject} {html_content}"
    vars_found = set(re.findall(r'\{\{\s*([a-zA-Z0-9_-]+)\s*\}\}', combined_text))

    missing_password = 0
    missing_job_id = 0
    missing_first_name = 0

    for c in contacts:
        if 'login_password' in vars_found and not c.login_password:
            missing_password += 1
        if 'job_id' in vars_found and not c.job_id:
            missing_job_id += 1
        if 'first_name' in vars_found and not c.first_name and not c.name:
            missing_first_name += 1

    return {
        'total_contacts': len(contacts),
        'variables_used': list(vars_found),
        'missing_password_count': missing_password,
        'missing_job_id_count': missing_job_id,
        'missing_first_name_count': missing_first_name,
    }
