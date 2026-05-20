from jinja2 import Environment, FileSystemLoader, exceptions

env = Environment(loader=FileSystemLoader('templates'))
try:
    t = env.get_template('cart.html')
    print('Template parsed successfully.')
except exceptions.TemplateSyntaxError as e:
    print('TemplateSyntaxError:', e)
except Exception as e:
    print('Other error:', type(e).__name__, e)
