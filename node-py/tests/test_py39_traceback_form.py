"""Workers run under Xcode's Python 3.9 on macOS. traceback.format_exception(exc) (one argument) is
3.10+ only and raises TypeError on 3.9, which killed the worker at launch. Every call in the package
must use the (type, value, tb) form."""
import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / 'livestack_node'


def test_traceback_format_exception_uses_three_arg_form():
    offenders = []
    for path in PKG.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ('format_exception', 'print_exception')
                    and getattr(node.func.value, 'id', None) == 'traceback'
                    and len(node.args) + len(node.keywords) < 3):
                offenders.append(f'{path.relative_to(PKG.parent)}:{node.lineno}')
    assert not offenders, f'Python 3.9-incompatible traceback call: {offenders}'
