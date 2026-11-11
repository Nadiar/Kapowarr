"""The OpenAPI spec must document every operation the API serves."""

import re
import sys
import unittest
from os.path import dirname, join
from unittest.mock import MagicMock

if 'grp' not in sys.modules:
    sys.modules['grp'] = MagicMock()

import yaml
from flask import Flask

from frontend.api import api
from frontend.ui import ui

REPO_ROOT = dirname(dirname(dirname(__file__)))
SPEC_PATH = join(REPO_ROOT, 'frontend', 'static', 'openapi.yaml')
TEMPLATE_FOLDER = join(REPO_ROOT, 'frontend', 'templates')
HTTP_METHODS = {'get', 'put', 'post', 'delete', 'patch'}


def load_spec() -> dict:
    with open(SPEC_PATH, encoding='utf-8') as spec_file:
        return yaml.safe_load(spec_file)


def resolve_ref(spec: dict, node: dict) -> dict:
    if '$ref' not in node:
        return node
    target = spec
    for part in node['$ref'].lstrip('#/').split('/'):
        target = target[part]
    return target


def served_operations() -> set:
    app = Flask(__name__)
    app.register_blueprint(api, url_prefix='/api')
    operations = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith('/api/'):
            continue
        path = re.sub(r'<(?:\w+:)?(\w+)>', r'{\1}', rule.rule[len('/api'):])
        for method in rule.methods or ():
            if method.lower() in HTTP_METHODS:
                operations.add((method.lower(), path))
    return operations


def documented_operations(spec: dict) -> set:
    return {
        (method, path)
        for path, item in spec['paths'].items()
        for method in item
        if method in HTTP_METHODS
    }


class TestOpenApiSpec(unittest.TestCase):
    def test_spec_documents_every_served_operation(self):
        missing = served_operations() - documented_operations(load_spec())
        self.assertEqual(sorted(missing), [])

    def test_spec_documents_no_unserved_operation(self):
        extra = documented_operations(load_spec()) - served_operations()
        self.assertEqual(sorted(extra), [])

    def test_every_operation_has_summary_and_responses(self):
        spec = load_spec()
        for method, path in documented_operations(spec):
            operation = spec['paths'][path][method]
            with self.subTest(method=method, path=path):
                self.assertTrue(operation.get('summary'))
                self.assertTrue(operation.get('responses'))

    def test_path_parameters_are_declared(self):
        spec = load_spec()
        for method, path in documented_operations(spec):
            names = set(re.findall(r'{(\w+)}', path))
            item = spec['paths'][path]
            parameters = [
                resolve_ref(spec, p)
                for p in item.get('parameters', [])
                + item[method].get('parameters', [])
            ]
            declared = {p['name'] for p in parameters if p['in'] == 'path'}
            with self.subTest(method=method, path=path):
                self.assertEqual(names, declared)

    def test_docs_page_loads_spec_relative_to_url_base(self):
        app = Flask(__name__, template_folder=TEMPLATE_FOLDER)
        app.register_blueprint(ui)
        with unittest.mock.patch(
            'frontend.ui.Server'
        ) as server:
            server.url_base = '/kapowarr'
            response = app.test_client().get('/api-docs')

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'/kapowarr/static/openapi.yaml',
            response.data
        )


if __name__ == '__main__':
    unittest.main()
