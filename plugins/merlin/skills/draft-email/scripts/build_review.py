#!/usr/bin/env python3
"""Build the review page for one rendered email, so the user sees it before anything is sent.

    python3 build_review.py DRAFT.md --about-me ABOUT_ME_FOLDER [--template PAGE.html] [--note "..."]
    python3 build_review.py --body EMAIL.html --to a@x.com [--to b@y.com] --subject "..." [--from "..."]
                            --about-me ABOUT_ME_FOLDER [--template PAGE.html] [--note "..."]

The second form is for an email built without a .md draft (a meeting recap's email.html): the
page is written to EMAIL_review.html beside it.

DRAFT.md is a draft that render_email.py (or the praxis send-email renderer) has already built:
DRAFT.html must sit beside it. The page shows To, Cc, From, Subject and the rendered body exactly
as it goes out, with copy buttons, and is written to DRAFT_review.html beside the draft. Nothing
else is written. Files listed under attachments: (paths relative to the draft's own folder, as the
send-email renderer reads them) show in an Attached row, each a link that opens the file in a new
tab with its size; a file that is not there shows as missing.

The page comes from --template, else ABOUT_ME_FOLDER/../templates/email-paste-artifact.html (the
Praxis install), else a plain page built in. Prints one JSON object: review, template, to,
subject, attachments. A refusal prints a plain reason on standard error, writes nothing and exits 2.
Standard library only.
"""

import argparse
import html
import json
import os
import re
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_email import Refusal, addresses, parse_draft, unquote  # noqa: E402

PASTE_REL = os.path.join('..', 'templates', 'email-paste-artifact.html')
TOKEN = re.compile(r'\[(CLIENT_NAME|SUBJECT_SHORT|EMAIL_KIND|SUMMARY_LINE|TO_EMAIL|SUBJECT|EMAIL_BODY)\]')
STEPS = re.compile(r'<ol class="steps">.*?</ol>', re.S)
OUR_STEPS = ('<ol class="steps">\n'
             '    <li>Read the email below. It is exactly what goes out, signature included.</li>\n'
             '    <li>Answer in the Claude chat. Nothing is sent without your yes there.</li>\n'
             '    <li>The Copy buttons are only for pasting it into Gmail yourself.</li>\n'
             '  </ol>')

PLAIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>[CLIENT_NAME] &middot; [SUBJECT_SHORT]</title>
<style>
body{margin:0;background:#f4f4f2;color:#222;font-family:system-ui,-apple-system,'Segoe UI',sans-serif;padding:24px 16px}
.wrap{max-width:760px;margin:0 auto}h1{font-size:20px;margin:0 0 4px}.sub{color:#555;font-size:13px;margin:0 0 16px}
.meta{background:#fff;border:1px solid #ddd;border-radius:8px;padding:4px 14px;margin:0 0 16px}
.mrow{display:flex;gap:12px;padding:9px 0;border-bottom:1px solid #eee;font-size:14px}.mrow:last-child{border-bottom:0}
.mlab{flex:0 0 72px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#777}
#emailBody{background:#fff;border:1px solid #ddd;border-radius:8px}
</style></head><body><div class="wrap">
<h1>[EMAIL_KIND] &middot; [CLIENT_NAME]</h1>
<p class="sub">[SUMMARY_LINE]</p>
<ol class="steps"></ol>
<div class="meta">
<div class="mrow"><span class="mlab">To</span><span>[TO_EMAIL]</span></div>
<div class="mrow"><span class="mlab">Subject</span><span>[SUBJECT]</span></div>
</div>
<div id="emailBody">[EMAIL_BODY]</div>
</div></body></html>
"""


SUBJECT_ROW = re.compile(r'<div class="mrow">[^\n]*?\[SUBJECT\][^\n]*?</div>')
LINK_STYLE = 'color:var(--orange-d,#1a5fb4);text-decoration:underline'
MISSING_STYLE = 'color:var(--mute,#777)'


def esc(s):
    return html.escape(s, quote=True)


def body_of(rendered):
    m = re.search(r'<body[^>]*>(.*)</body>', rendered, re.S | re.I)
    return (m.group(1) if m else rendered).strip('\n')


def display_name(named):
    m = re.match(r'^\s*"?([^"<]*?)"?\s*<', named)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return named.split('@')[0]


def file_size(n):
    if n < 1024:
        return '%d bytes' % n
    if n < 1024 * 1024:
        return '%.1f KB' % (n / 1024)
    return '%.1f MB' % (n / (1024 * 1024))


def attachment_items(fm, draft_dir, page_dir):
    """One entry per attachments: line item. Paths are relative to the draft's own folder, the same
    rule as the send-email renderer. A file that is not there is reported, never a crash."""
    out = []
    for item in (x.strip() for x in fm.get('attachments', '').split(',')):
        item = unquote(item)
        if not item:
            continue
        path = os.path.normpath(os.path.join(draft_dir, os.path.expanduser(item)))
        found = os.path.isfile(path)
        href = os.path.relpath(path, page_dir).replace(os.sep, '/')
        out.append({'name': os.path.basename(path), 'path': path, 'href': quote(href, safe='/'),
                    'size': os.path.getsize(path) if found else None})
    return out


def attachments_row(items):
    """An Attached row for the To/Subject panel: each file opens in a new tab (a PDF shows inline)."""
    lines = []
    for a in items:
        if a['size'] is None:
            lines.append('<div>%s <span style="%s">&middot; missing</span></div>' % (esc(a['name']), MISSING_STYLE))
        else:
            lines.append('<div><a href="%s" target="_blank" rel="noopener" style="%s">%s</a> '
                         '<span style="%s">&middot; %s</span></div>'
                         % (esc(a['href']), LINK_STYLE, esc(a['name']), MISSING_STYLE, file_size(a['size'])))
    return ('<div class="mrow attachments"><span class="mlab">Attached</span><span class="mval">%s</span></div>'
            % ''.join(lines))


def build(draft, about_me, template=None, note=None, direct=None):
    if direct:
        rendered_path = direct['body']
        if not rendered_path.endswith('.html') or not os.path.isfile(rendered_path):
            raise Refusal('There is no built email at %s. Nothing was written.' % rendered_path)
        fm = {'to': ', '.join(direct['to']), 'subject': direct['subject'], 'from': direct['from'] or ''}
        out = rendered_path[:-5] + '_review.html'
    else:
        if not draft or not draft.endswith('.md') or not os.path.isfile(draft):
            raise Refusal('There is no draft at %s. Nothing was written.' % draft)
        rendered_path = draft[:-3] + '.html'
        if not os.path.isfile(rendered_path):
            raise Refusal('The email has not been built yet: there is no %s. Build it first, then make the '
                          'review page. Nothing was written.' % rendered_path)
        fm, _ = parse_draft(draft)
        out = draft[:-3] + '_review.html'
    attachments = attachment_items(fm, os.path.dirname(draft), os.path.dirname(out)) if not direct else []
    to, to_named = addresses(fm, 'to', True)
    cc, cc_named = addresses(fm, 'cc', False)
    subject = unquote(fm.get('subject', ''))
    if not subject:
        raise Refusal('The email has no subject. Nothing was written.')
    sender = unquote(fm.get('from', ''))
    if template is None:
        guess = os.path.normpath(os.path.join(about_me, PASTE_REL)) if about_me else None
        template = guess if guess and os.path.isfile(guess) else None
    if template:
        with open(template, encoding='utf-8') as f:
            page = f.read()
        # The template's own instructions name every token, so they go before anything is filled.
        page = re.sub(r'<!--.*?-->\n?', '', page, count=1, flags=re.S)
        page = page.replace('Merlin [VERSION] &middot; ', '')
    else:
        page = PLAIN_PAGE
    page = STEPS.sub(lambda m: OUR_STEPS, page, count=1)
    if attachments:
        row = attachments_row(attachments)
        page, n = SUBJECT_ROW.subn(lambda m: m.group(0) + '\n    ' + row, page, count=1)
        if not n:
            # A template with no Subject row: the attachments go right above the email body.
            page = page.replace('<div id="emailBody">', '<div class="meta">%s</div>\n<div id="emailBody">' % row, 1)
    with open(rendered_path, encoding='utf-8') as f:
        email_body = body_of(f.read())
    summary = [note or 'not yet sent']
    if cc_named:
        summary.append('Cc: ' + ', '.join(cc_named))
    if sender:
        summary.append('From: ' + sender)
    fill = {
        'CLIENT_NAME': esc(', '.join(display_name(n) for n in to_named)),
        'SUBJECT_SHORT': esc(subject[:40]),
        'EMAIL_KIND': 'Email',
        'SUMMARY_LINE': ' &middot; '.join(esc(s) for s in summary),
        'TO_EMAIL': esc(', '.join(to)),
        'SUBJECT': esc(subject),
        'EMAIL_BODY': email_body,
    }
    # One pass, so text inside the email body is never scanned for a token.
    page = TOKEN.sub(lambda m: fill[m.group(1)], page)
    return out, page, template, to, subject, attachments


def main(argv=None):
    ap = argparse.ArgumentParser(description='Build the review page for one rendered email.')
    ap.add_argument('draft', nargs='?')
    ap.add_argument('--body', default=None, help='a built email .html with no .md draft')
    ap.add_argument('--to', action='append', default=[])
    ap.add_argument('--subject', default=None)
    ap.add_argument('--from', dest='sender', default=None)
    ap.add_argument('--about-me', default=None)
    ap.add_argument('--template', default=None)
    ap.add_argument('--note', default=None, help='one line under the heading, e.g. "goes out through Resend"')
    a = ap.parse_args(argv)
    if bool(a.draft) == bool(a.body):
        sys.stderr.write('draft-email: give either a draft .md or --body, not both and not neither.\n')
        return 2
    direct = None
    if a.body:
        direct = {'body': os.path.abspath(os.path.expanduser(a.body)), 'to': a.to, 'subject': a.subject or '',
                  'from': a.sender}
    draft = os.path.abspath(os.path.expanduser(a.draft)) if a.draft else None
    about_me = os.path.abspath(os.path.expanduser(a.about_me)) if a.about_me else None
    template = os.path.abspath(os.path.expanduser(a.template)) if a.template else None
    try:
        out, page, used, to, subject, attachments = build(draft, about_me, template, a.note, direct)
    except Refusal as err:
        sys.stderr.write('draft-email: %s\n' % err)
        return 2
    except OSError as err:
        sys.stderr.write('draft-email: Merlin could not read %s (technical detail: %s). Nothing was written.\n'
                         % (err.filename or 'one of the files', err.strerror or err))
        return 2
    with open(out, 'w', encoding='utf-8', newline='\n') as f:
        f.write(page)
    sys.stdout.write(json.dumps({'review': out, 'template': used, 'to': to, 'subject': subject,
                                 'attachments': [{'name': a['name'], 'path': a['path'], 'href': a['href'],
                                                  'missing': a['size'] is None} for a in attachments]},
                                ensure_ascii=False) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
