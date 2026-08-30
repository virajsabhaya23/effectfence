from pathlib import Path
import json, xml.etree.ElementTree as ET


def junit(result, path):
    # JUnit is what most CI dashboards actually show – keep it small but
    # include the certificate so a failing run can be tied back to the JSON.
    safe = bool(result.get("safe"))
    violations = result.get("violations") or []
    scenario_id = str(result.get("scenario_id", result.get("id", "effectfence")))
    suite = ET.Element(
        "testsuite",
        {
            "name": "effectfence",
            "tests": "1",
            "failures": "0" if safe else "1",
            "errors": "0",
            "time": "0",
        },
    )
    props = ET.SubElement(suite, "properties")
    # keep the hash visible in CI without digging into JSON
    cert = result.get("certificate_sha256") or result.get("certificateSha256") or ""
    if cert:
        ET.SubElement(props, "property", {"name": "certificate_sha256", "value": str(cert)})
    tc = ET.SubElement(suite, "testcase", classname="effectfence", name=scenario_id, time="0")
    if not safe:
        msg = "; ".join(v.get("kind", "violation") for v in violations) or "unsafe schedule"
        failure = ET.SubElement(tc, "failure", message=msg[:500])
        # failure text is the only place we can put structured detail that
        # Jenkins/GitLab will preserve
        failure.text = json.dumps(violations, indent=2)
    else:
        # still record the certificate in system-out for pass cases
        if cert:
            out = ET.SubElement(tc, "system-out")
            out.text = f"certificate_sha256={cert}"
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(suite)
    try:
        ET.indent(tree, space="  ")
    except Exception:
        pass
    tree.write(dest, encoding="utf-8", xml_declaration=True)
