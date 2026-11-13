"""Offline standalone metadata and supplied public asset contract."""
from html.parser import HTMLParser
import json
from pathlib import Path
import struct
import unittest

import jinja2
from ansible.module_utils.parsing.convert_bool import boolean

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'files/static'


class Head(HTMLParser):
    def __init__(self, page):
        super().__init__()
        self.links, self.meta = [], {}
        self.feed(page)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link':
            self.links.append(attrs)
        if tag == 'meta':
            self.meta[attrs.get('name')] = attrs.get('content')


class PWATests(unittest.TestCase):
    def test_head_metadata_in_dashboard_and_landing_page(self):
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters.update(to_json=json.dumps, bool=boolean)
        template = env.from_string((ROOT / 'templates/index.html.j2').read_text())
        for enabled in (True, False):
            with self.subTest(dashboard=enabled):
                page = template.render(desk_dashboard_enabled=enabled, dashboard_members=[])
                head = Head(page)
                for link in [
                    {'rel': 'icon', 'href': '/favicon.svg', 'type': 'image/svg+xml'},
                    {'rel': 'icon', 'href': '/favicon.ico', 'sizes': 'any'},
                    {'rel': 'apple-touch-icon', 'href': '/apple-touch-icon.png'},
                    {'rel': 'manifest', 'href': '/manifest.webmanifest'},
                ]:
                    self.assertIn(link, head.links)
                    self.assertTrue((ASSETS / link['href'][1:]).is_file())
                for name, value in {
                    'theme-color': '#100d08', 'apple-mobile-web-app-capable': 'yes',
                    'apple-mobile-web-app-title': 'Desk Cluster',
                    'apple-mobile-web-app-status-bar-style': 'black-translucent',
                    'viewport': 'width=device-width,initial-scale=1',
                }.items():
                    self.assertEqual(head.meta[name], value)
                self.assertNotIn('serviceWorker', page)

    def test_manifest_and_icon_files(self):
        manifest = json.loads((ASSETS / 'manifest.webmanifest').read_text())
        for key, value in {
            'name': 'Desk Cluster', 'short_name': 'Desk Cluster', 'start_url': '/',
            'scope': '/', 'display': 'standalone', 'background_color': '#100d08',
            'theme_color': '#100d08',
        }.items():
            self.assertEqual(manifest[key], value)
        self.assertEqual(len(manifest['icons']), 2)
        for size, icon in zip((192, 512), manifest['icons']):
            self.assertEqual(icon['sizes'], f'{size}x{size}')
            self.assertEqual(icon['type'], 'image/png')
            self.assertEqual(set(icon['purpose'].split()), {'any', 'maskable'})
            raw = (ASSETS / icon['src'].removeprefix('/')).read_bytes()
            self.assertEqual(raw[:8], b'\x89PNG\r\n\x1a\n')
            self.assertEqual(struct.unpack('>II', raw[16:24]), (size, size))
        raw = (ASSETS / 'apple-touch-icon.png').read_bytes()
        self.assertEqual(struct.unpack('>II', raw[16:24]), (180, 180))


if __name__ == '__main__':
    unittest.main()
