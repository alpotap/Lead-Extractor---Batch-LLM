import re, os, tempfile, sys

content = open('static/index.html', encoding='utf-8').read()
scripts = re.findall(r'<script>(.*?)</script>', content, re.DOTALL)

for i, s in enumerate(scripts):
    f = tempfile.NamedTemporaryFile(suffix='.mjs', delete=False, mode='w', encoding='utf-8')
    f.write(s)
    f.close()
    r = os.system(f'node --check "{f.name}"')
    if r != 0:
        print(f'SYNTAX ERROR in script block {i}')
        sys.exit(1)
    else:
        print(f'Script block {i}: OK ({len(s)} chars)')
    os.unlink(f.name)
print('All OK')
