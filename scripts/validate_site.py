from pathlib import Path
from html.parser import HTMLParser
import argparse,hashlib,json,re,sys,zipfile
ap=argparse.ArgumentParser(); ap.add_argument('--require-generated',action='store_true'); ap.add_argument('--root',default='.')
a=ap.parse_args(); ROOT=Path(a.root).resolve()
(ROOT/'site-validation-report.json').write_text('{}',encoding='utf-8')
static=['index.html','resume.html','styles.css','contact.vcf','profile.json','resume.txt','robots.txt','sitemap.xml','feed.xml','llms.txt','site.webmanifest','favicon.svg','og-card.svg','contact-qr.svg']
generated=['og-card.png','Pritam_Warghade_Global_Piping_CV_2026.pdf','Pritam_Warghade_Global_Piping_CV_2026.docx','Pritam_Warghade_Public_CV.pdf','Pritam_Warghade_Public_CV.docx']
required=static+(generated if a.require_generated else [])
errors=[]; report={'required_files':{},'internal_links':[],'checks':{'require_generated':a.require_generated}}
for name in required:
 p=ROOT/name; ok=p.is_file() and p.stat().st_size>0; report['required_files'][name]={'exists':ok,'bytes':p.stat().st_size if p.exists() else 0}
 if not ok: errors.append(f'missing/empty: {name}')
phone='+91 9172645866'; email='warghade2012@gmail.com'; canonical='https://psw2025-cmd.github.io/PRITAM/'
for name in ['index.html','resume.html','profile.json','resume.txt','contact.vcf']:
 text=(ROOT/name).read_text(encoding='utf-8',errors='replace')
 if phone not in text and '+919172645866' not in text: errors.append(f'phone missing in {name}')
 if email not in text: errors.append(f'email missing in {name}')
for term in ['aadhaar','pan number','passport number','date of birth','bank account']:
 for name in ['index.html','resume.html','profile.json','resume.txt']:
  if term in (ROOT/name).read_text(encoding='utf-8',errors='ignore').lower(): errors.append(f'forbidden sensitive term {term} in {name}')
class P(HTMLParser):
 def __init__(self): super().__init__(); self.links=[]; self.img_alt=[]
 def handle_starttag(self,tag,attrs):
  d=dict(attrs)
  if tag=='a' and d.get('href'): self.links.append(d['href'])
  if tag=='img': self.img_alt.append(d.get('alt',''))
for name in ['index.html','resume.html','404.html']:
 p=P(); p.feed((ROOT/name).read_text(encoding='utf-8'))
 for href in p.links:
  if href.startswith(('http://','https://','mailto:','tel:','#')): continue
  target=href.split('#',1)[0].split('?',1)[0]
  if not target: continue
  exists=(ROOT/target).exists() or (not a.require_generated and target in generated) or target=='site-validation-report.json'
  report['internal_links'].append({'from':name,'href':href,'exists':exists})
  if not exists: errors.append(f'broken internal link: {name} -> {href}')
 if name=='index.html' and any(not x.strip() for x in p.img_alt): errors.append('image without alt text in index.html')
idx=(ROOT/'index.html').read_text(encoding='utf-8')
for token in ['application/ld+json','og-card.png','Pritam_Warghade_Global_Piping_CV_2026.pdf','contact-qr.svg',canonical]:
 if token not in idx: errors.append(f'index missing token: {token}')
if a.require_generated:
 pdf=(ROOT/'Pritam_Warghade_Global_Piping_CV_2026.pdf').read_bytes()
 if not pdf.startswith(b'%PDF-'): errors.append('invalid PDF signature')
 pages=len(re.findall(rb'/Type\s*/Page\b',pdf)); report['checks']['pdf_page_markers']=pages
 if pages<4: errors.append(f'PDF page markers below expected: {pages}')
 try:
  with zipfile.ZipFile(ROOT/'Pritam_Warghade_Global_Piping_CV_2026.docx') as z:
   if 'word/document.xml' not in set(z.namelist()): errors.append('DOCX missing word/document.xml')
 except Exception as e: errors.append(f'invalid DOCX: {e}')
for name in required:
 p=ROOT/name
 if p.exists(): report['required_files'][name]['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
report['checks']['errors']=errors; report['checks']['status']='PASS' if not errors else 'FAIL'
(ROOT/'site-validation-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report['checks'],indent=2))
if errors:
 for e in errors: print('ERROR:',e,file=sys.stderr)
 raise SystemExit(1)
