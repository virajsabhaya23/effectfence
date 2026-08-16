from pathlib import Path
import json, xml.etree.ElementTree as ET

def junit(result,path):
    suite=ET.Element('testsuite',name='effectfence',tests='1',failures='0' if result['safe'] else '1')
    tc=ET.SubElement(suite,'testcase',classname='effectfence',name=result['scenario_id'])
    if not result['safe']:
        f=ET.SubElement(tc,'failure',message='; '.join(v['kind'] for v in result['violations'])); f.text=json.dumps(result['violations'])
    Path(path).parent.mkdir(parents=True,exist_ok=True); ET.ElementTree(suite).write(path,encoding='unicode')
