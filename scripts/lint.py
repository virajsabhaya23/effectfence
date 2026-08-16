from pathlib import Path
import ast,sys
bad=[]
for base in ('effectfence','tests'):
 for p in Path(base).rglob('*.py'):
  text=p.read_text()
  try: ast.parse(text)
  except SyntaxError as e: bad.append(f'{p}:{e}')
  for i,line in enumerate(text.splitlines(),1):
   if '\t' in line: bad.append(f'{p}:{i}: tab')
   if line.rstrip()!=line: bad.append(f'{p}:{i}: trailing whitespace')
if bad: print('\n'.join(bad));sys.exit(1)
print('lint: PASS')
