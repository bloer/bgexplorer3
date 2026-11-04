""" The versioning package depends on nothing else in bgexplorer """
import ast
import pathlib
import re
import subprocess
import sys
import unittest

PACKAGE = pathlib.Path(__file__).parent.parent / 'src/bgexplorer/versioning'
ALLOWED_EXTERNAL = {'mongoengine', 'bson', 'pymongo', 'blinker'}

DOMAIN_WORDS = ('component', 'sourceterm', 'hiteff', 'calc_token',
                'attachments', 'rois', 'emissionspec', 'bgexplorer_')


def imported_modules(path: pathlib.Path):
    """ (absolute module name, line) of every import in the file """
    module = ['bgexplorer', 'versioning', path.stem]
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module[:len(module) - node.level]
                name = '.'.join(base + ([node.module] if node.module else []))
            else:
                name = node.module
            yield name, node.lineno
            if node.module is None:
                # `from . import x` imports the module x
                for alias in node.names:
                    yield f'{name}.{alias.name}', node.lineno


def outside_imports():
    found = {}
    for path in sorted(PACKAGE.glob('*.py')):
        for name, line in imported_modules(path):
            top = name.split('.')[0]
            if name.startswith('bgexplorer.versioning'):
                continue
            if top in sys.stdlib_module_names or top in ALLOWED_EXTERNAL:
                continue
            found.setdefault(name, []).append(f'{path.name}:{line}')
    return found


class TestIsolation(unittest.TestCase):
    def test_no_unexpected_imports(self):
        found = outside_imports()
        self.assertEqual(found, {})

    def test_clean_interpreter(self):
        """ importing the package loads none of the application """
        code = ("import sys, bgexplorer.versioning; "
                "bad = [m for m in sys.modules if m.split('.')[0] in "
                "('flask', 'pint') or m.startswith(('bgexplorer.models', "
                "'bgexplorer.application'))]; "
                "print(bad); sys.exit(bool(bad))")
        done = subprocess.run([sys.executable, '-c', code],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_no_domain_words(self):
        """ the package doesn't know about what it versions """
        pattern = re.compile('|'.join(DOMAIN_WORDS), re.IGNORECASE)
        found = {}
        for path in sorted(PACKAGE.glob('*.py')):
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pattern.search(line):
                    found.setdefault(path.name, []).append(n)
        self.assertEqual(found, {})
