from pathlib import Path

import os
import re
import json
import base64
import html
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

JOB_NAME = "OpenCart-Selenium-UI-Tests"
OUTPUT_DIR = Path(__file__).resolve().parent
SUREFIRE_FILENAME = "TEST-TestSuite.xml"
TESTNG_FILENAME = "testng-results.xml"
HISTORY_LIMIT = 20


def jenkins_request(url):
    username = os.getenv("JENKINS_USER")
    token = os.getenv("JENKINS_API_TOKEN")
    if not username or not token:
        raise RuntimeError("JENKINS_USER veya JENKINS_API_TOKEN eksik.")
    credentials = base64.b64encode(
        f"{username}:{token}".encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Basic {credentials}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def collect_jenkins_data():
    base_url = os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")
    job_url = f"{base_url}/job/{JOB_NAME}"
    print("\n===== JENKINS VERI TOPLAMA =====")

    job_data = json.loads(jenkins_request(f"{job_url}/api/json"))
    last_build = job_data.get("lastCompletedBuild")
    if not last_build:
        raise RuntimeError("Jenkins'te tamamlanmış bir build bulunamadı.")

    build_number = last_build["number"]
    build_url = f"{job_url}/{build_number}"
    build_data = json.loads(jenkins_request(
        f"{build_url}/api/json?tree=number,result,url,building,timestamp,duration,displayName"
    ))
    if build_data.get("building"):
        raise RuntimeError("Seçilen build hâlâ çalışıyor; rapor hazırlanmadı.")
    if not build_data.get("result"):
        raise RuntimeError("Build sonucu alınamadı; rapor hazırlanmadı.")

    # Geçmiş build metadata'sı. Herhangi bir build'in raporu yoksa geçmiş tablosunda
    # test sayıları '—' gösterilir; sıfırmış gibi varsayılmaz.
    history_url = (
        f"{job_url}/api/json?tree=builds[number,result,url,timestamp,duration,building]"
        f"{{0,{HISTORY_LIMIT}}}"
    )
    try:
        history_data = json.loads(jenkins_request(history_url))
        build_history = [
            item for item in history_data.get("builds", [])
            if not item.get("building") and item.get("result")
        ]
    except Exception as exc:
        print(f"Geçmiş build listesi alınamadı: {exc}")
        build_history = []

    prefix = f"jenkins_build_{build_number}"
    console_path = OUTPUT_DIR / f"{prefix}_jenkins_console.log"
    surefire_path = OUTPUT_DIR / f"{prefix}_{SUREFIRE_FILENAME}"
    testng_path = OUTPUT_DIR / f"{prefix}_{TESTNG_FILENAME}"

    console = jenkins_request(f"{build_url}/consoleText").decode(
        "utf-8", errors="replace"
    )
    console_path.write_text(console, encoding="utf-8")

    surefire_url = f"{build_url}/artifact/target/surefire-reports/{SUREFIRE_FILENAME}"
    surefire_path.write_bytes(jenkins_request(surefire_url))

    try:
        testng_url = f"{build_url}/artifact/target/surefire-reports/{TESTNG_FILENAME}"
        testng_path.write_bytes(jenkins_request(testng_url))
    except Exception as exc:
        print(f"TestNG raporu indirilemedi; isteğe bağlı rapor atlandı: {exc}")
        testng_path = None

    # Her geçmiş build için Surefire özeti almayı dene.
    for item in build_history:
        item["tests"] = None
        item["failures"] = None
        item["errors"] = None
        item["skipped"] = None
        item["passed"] = None
        item["test_time"] = None
        history_build_url = item.get("url") or f"{job_url}/{item['number']}/"
        history_report_url = (
            f"{history_build_url.rstrip('/')}/artifact/target/surefire-reports/"
            f"{SUREFIRE_FILENAME}"
        )
        try:
            root = ET.fromstring(jenkins_request(history_report_url))
            tests = int(root.attrib.get("tests", 0))
            failures = int(root.attrib.get("failures", 0))
            errors = int(root.attrib.get("errors", 0))
            skipped = int(root.attrib.get("skipped", 0))
            item.update({
                "tests": tests,
                "failures": failures,
                "errors": errors,
                "skipped": skipped,
                "passed": max(0, tests - failures - errors - skipped),
                "test_time": float(root.attrib.get("time", 0)),
            })
        except Exception:
            # Jenkins artifact retention veya yol farklılığı nedeniyle rapor olmayabilir.
            pass

    print(f"Build: {build_number} | Sonuç: {build_data['result']}")
    print(f"Build URL: {build_data.get('url', build_url)}")
    print(f"Konsol: {console_path}")
    print(f"Surefire XML: {surefire_path}")
    if testng_path:
        print(f"TestNG XML: {testng_path}")

    return {
        "number": build_number,
        "result": build_data["result"],
        "url": build_data.get("url", build_url),
        "timestamp": build_data.get("timestamp"),
        "duration_ms": build_data.get("duration"),
        "console_path": console_path,
        "surefire_path": surefire_path,
        "testng_path": testng_path,
        "build_history": build_history,
    }


def parse_surefire(path):
    root = ET.parse(path).getroot()
    summary = {
        "suite": root.attrib.get("name", "Unknown suite"),
        "tests": int(root.attrib.get("tests", 0)),
        "failures": int(root.attrib.get("failures", 0)),
        "errors": int(root.attrib.get("errors", 0)),
        "skipped": int(root.attrib.get("skipped", 0)),
        "time": float(root.attrib.get("time", 0)),
    }
    failed_tests = []
    for case in root.findall(".//testcase"):
        issue = case.find("failure")
        if issue is None:
            issue = case.find("error")
        if issue is not None:
            failed_tests.append({
                "name": case.attrib.get("name", "Unknown test"),
                "class": case.attrib.get("classname", "Unknown class"),
                "type": issue.tag,
                "message": issue.attrib.get("message", "No failure message"),
                "details": (issue.text or "").strip()[:2500],
            })
    return {"summary": summary, "failed_tests": failed_tests}


def parse_testng(path):
    if not path or not Path(path).exists():
        return None
    try:
        root = ET.parse(path).getroot()
        return {
            "total": int(root.attrib.get("total", 0)),
            "passed": int(root.attrib.get("passed", 0)),
            "failed": int(root.attrib.get("failed", 0)),
            "skipped": int(root.attrib.get("skipped", 0)),
            "ignored": int(root.attrib.get("ignored", 0)),
        }
    except (ET.ParseError, OSError, ValueError):
        return None


def parse_console_summaries(path):
    content = Path(path).read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),\s*Skipped:\s*(\d+)(?:.*?--\s*in\s*(?P<suite>.+))?\s*$",
        re.IGNORECASE,
    )
    summaries = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        match = pattern.search(line)
        if match:
            summaries.append({
                "line": line_number,
                "suite": match.group("suite").strip() if match.group("suite") else None,
                "tests": int(match.group(1)),
                "failures": int(match.group(2)),
                "errors": int(match.group(3)),
                "skipped": int(match.group(4)),
            })
    return summaries


def fmt_time(timestamp_ms):
    if not timestamp_ms:
        return "Bilinmiyor"
    return datetime.fromtimestamp(timestamp_ms / 1000).astimezone().strftime(
        "%d.%m.%Y %H:%M:%S %Z"
    )


def fmt_duration(duration_ms):
    if duration_ms is None:
        return "Bilinmiyor"
    seconds = round(duration_ms / 1000)
    return f"{seconds // 60} dk {seconds % 60} sn" if seconds >= 60 else f"{seconds} sn"


def status_label(result):
    return {
        "SUCCESS": ("BAŞARILI", "success"),
        "FAILURE": ("BAŞARISIZ", "failure"),
        "UNSTABLE": ("KISMEN BAŞARISIZ", "unstable"),
        "ABORTED": ("İPTAL EDİLDİ", "aborted"),
    }.get(result, (result or "BİLİNMİYOR", "unknown"))


def build_report_markdown(build, surefire, testng, summaries, html_path):
    s = surefire["summary"]
    status, _ = status_label(build["result"])
    lines = [
        f"# Jenkins Build #{build['number']} — Kısa Rapor", "",
        f"- **Sonuç:** {status} ({build['result']})",
        f"- **Build zamanı:** {fmt_time(build['timestamp'])}",
        f"- **Süre:** {fmt_duration(build['duration_ms'])}",
        f"- **Jenkins:** {build['url']}", "",
        "## Test özeti (Surefire XML)",
        f"- Toplam: {s['tests']}", f"- Başarılı: {max(0, s['tests'] - s['failures'] - s['errors'] - s['skipped'])}",
        f"- Başarısız: {s['failures']}", f"- Hata: {s['errors']}",
        f"- Atlanan: {s['skipped']}", f"- Test süresi: {s['time']:.1f} sn", "",
        "## Başarısız testler",
    ]
    if surefire["failed_tests"]:
        for item in surefire["failed_tests"]:
            lines += [
                f"### {item['class']}.{item['name']}",
                f"- **Tür:** {item['type']}",
                f"- **Mesaj:** {item['message']}", "",
            ]
    else:
        lines.append("Surefire XML raporunda başarısız test kaydı bulunamadı.")
    if testng:
        lines += [
            "", "## TestNG özeti (ayrı kaynak)",
            f"- Toplam: {testng['total']}", f"- Başarılı: {testng['passed']}",
            f"- Başarısız: {testng['failed']}", f"- Atlanan: {testng['skipped']}",
            f"- Yoksayılan: {testng['ignored']}",
        ]
    if summaries:
        lines += ["", "## Konsolda bulunan özetler"]
        for item in summaries:
            scope = f"suite: {item['suite']}" if item["suite"] else "kapsamı belirtilmemiş"
            lines.append(
                f"- Satır {item['line']} ({scope}): {item['tests']} test, "
                f"{item['failures']} başarısız, {item['errors']} hata, "
                f"{item['skipped']} atlanan"
            )
    lines += ["", "> Bu rapor build ve test sonuçlarını özetler; kök neden analizi yapmaz.", ""]
    return "\n".join(lines)


def render_dashboard(build, surefire, testng, summaries, report_path, build_history):
    s = surefire["summary"]
    status_text, status_class = status_label(build["result"])
    total, failures, errors, skipped = s["tests"], s["failures"], s["errors"], s["skipped"]
    passed = max(0, total - failures - errors - skipped)

    def pct(value):
        return value / total * 100 if total else 0

    passed_pct, failed_pct, error_pct, skipped_pct = (
        pct(passed), pct(failures), pct(errors), pct(skipped)
    )
    donut = (
        f"conic-gradient(#16a36a 0% {passed_pct:.2f}%, "
        f"#ef4444 {passed_pct:.2f}% {passed_pct + failed_pct:.2f}%, "
        f"#64748b {passed_pct + failed_pct:.2f}% "
        f"{passed_pct + failed_pct + error_pct:.2f}%, "
        f"#f5b82e {passed_pct + failed_pct + error_pct:.2f}% 100%)"
    )
    escaped_url = html.escape(build["url"], quote=True)
    report_href = html.escape(Path(report_path).name, quote=True)

    fail_rows = []
    for item in surefire["failed_tests"]:
        fail_rows.append(f"""
        <tr><td><strong>{html.escape(item['class'])}</strong>
        <div class="sub">{html.escape(item['name'])}</div></td>
        <td><span class="pill red">{html.escape(item['type'].upper())}</span></td>
        <td class="message">{html.escape(item['message'])}</td>
        <td><details><summary>Ayrıntı</summary><pre>{html.escape(item['details'] or 'Ayrıntı yok.')}</pre></details></td></tr>""")
    if not fail_rows:
        fail_rows.append('<tr><td colspan="4" class="empty">Surefire XML raporunda başarısız test kaydı bulunamadı.</td></tr>')

    console_rows = []
    for item in summaries:
        scope = f"Suite: {item['suite']}" if item["suite"] else "Belirtilmemiş"
        console_rows.append(
            f"<tr><td>{item['line']}</td><td>{html.escape(scope)}</td>"
            f"<td>{item['tests']}</td><td>{item['failures']}</td>"
            f"<td>{item['errors']}</td><td>{item['skipped']}</td></tr>"
        )
    if not console_rows:
        console_rows.append('<tr><td colspan="6" class="empty">Konsolda test özeti bulunamadı.</td></tr>')

    history_rows = []
    for item in build_history:
        label, css = status_label(item.get("result"))
        number = item.get("number", "—")
        url = html.escape(item.get("url") or "#", quote=True)
        def val(key):
            return "—" if item.get(key) is None else str(item[key])
        history_rows.append(f"""
        <tr>
          <td><a href="{url}" target="_blank" rel="noreferrer">#{number}</a></td>
          <td><span class="status small {css}">{html.escape(label)}</span></td>
          <td>{html.escape(fmt_time(item.get('timestamp')))}</td>
          <td>{html.escape(fmt_duration(item.get('duration')))}</td>
          <td>{val('tests')}</td><td>{val('passed')}</td>
          <td>{val('failures')}</td><td>{val('errors')}</td><td>{val('skipped')}</td>
        </tr>""")
    if not history_rows:
        history_rows.append('<tr><td colspan="9" class="empty">Geçmiş build kaydı alınamadı.</td></tr>')

    testng_html = ""
    if testng:
        testng_html = f"""
        <section class="panel"><div class="head"><div><h2>TestNG raporu</h2>
        <p class="muted">Ayrı rapor kaynağı; Surefire sayılarıyla birleştirilmez.</p></div>
        <span class="pill">TestNG XML</span></div>
        <div class="metrics five">
          <div class="metric"><span>Toplam</span><strong>{testng['total']}</strong></div>
          <div class="metric green"><span>Başarılı</span><strong>{testng['passed']}</strong></div>
          <div class="metric red"><span>Başarısız</span><strong>{testng['failed']}</strong></div>
          <div class="metric amber"><span>Atlanan</span><strong>{testng['skipped']}</strong></div>
          <div class="metric"><span>Yoksayılan</span><strong>{testng['ignored']}</strong></div>
        </div></section>"""

    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>QA Agent | Build #{build['number']}</title>
<style>
:root{{--bg:#f3f6fb;--panel:#fff;--text:#172b4d;--muted:#64748b;--line:#e2e8f0;--blue:#2563eb;--green:#16a36a;--red:#ef4444;--amber:#f5b82e}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",Arial,sans-serif}}
.layout{{display:flex;min-height:100vh}}aside{{width:220px;flex-shrink:0;background:#12213d;color:#fff;padding:28px 16px}}
.brand{{font-size:22px;font-weight:800;margin-bottom:4px}}.brand-sub{{color:#b7c6df;font-size:12px;margin-bottom:34px}}
.nav{{display:block;padding:12px 13px;border-radius:9px;color:#dbe6f7;margin:6px 0;text-decoration:none}}.nav:hover,.nav.active{{background:#245bd6;color:white}}
main{{flex:1;min-width:0;max-width:1500px;padding:30px}}.topbar,.head{{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}}
h1{{margin:0;font-size:27px;letter-spacing:-.5px}}h2{{margin:0;font-size:17px}}p{{margin:6px 0}}.muted,.sub{{color:var(--muted);font-size:12px}}
.meta{{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:10px;color:var(--muted);font-size:12px}}
.status{{display:inline-block;padding:8px 13px;border-radius:999px;font-size:12px;font-weight:800;white-space:nowrap}}
.status.small{{padding:4px 8px;font-size:10px}}.success{{background:#dcfce7;color:#166534}}.failure{{background:#fee2e2;color:#991b1b}}.unstable{{background:#fef3c7;color:#92400e}}.aborted,.unknown{{background:#e2e8f0;color:#334155}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:21px;margin:18px 0;box-shadow:0 3px 12px #0f172a09}}
.metrics{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:13px;margin:18px 0}}
.metric{{background:#fff;border:1px solid var(--line);border-radius:13px;padding:17px;border-top:3px solid #cbd5e1}}
.metric.green{{border-top-color:var(--green)}}.metric.red{{border-top-color:var(--red)}}.metric.amber{{border-top-color:var(--amber)}}.metric.blue{{border-top-color:#60a5fa}}
.metric span{{display:block;color:var(--muted);font-size:12px}}.metric strong{{display:block;font-size:29px;margin-top:5px}}
.actions{{display:flex;flex-wrap:wrap;gap:9px;margin-top:15px}}.btn{{display:inline-block;text-decoration:none;padding:9px 13px;border:1px solid var(--line);border-radius:8px;color:var(--text);font-weight:650;background:#fff;font-size:12px}}.btn.primary{{background:var(--blue);color:#fff;border-color:var(--blue)}}
.charts{{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.25fr);gap:18px}}.charts .panel{{margin:0}}
.donut-area{{display:flex;justify-content:center;align-items:center;gap:25px;flex-wrap:wrap;padding:12px 0}}.donut{{width:185px;height:185px;border-radius:50%;background:{donut};position:relative;flex-shrink:0}}
.donut:after{{content:"";position:absolute;inset:29px;border-radius:50%;background:white}}.donut-center{{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:center;align-items:center;z-index:1}}.donut-center strong{{font-size:28px}}.donut-center span{{font-size:11px;color:var(--muted)}}
.legend{{min-width:130px}}.legend-row{{display:flex;align-items:center;gap:9px;margin:12px 0;font-size:12px}}.dot{{width:10px;height:10px;border-radius:50%;flex-shrink:0}}.legend-row strong{{margin-left:auto;padding-left:14px}}
.bars{{padding-top:10px}}.bar-row{{display:grid;grid-template-columns:82px minmax(40px,1fr) 34px;align-items:center;gap:10px;margin:17px 0;font-size:12px}}.bar-track{{height:15px;background:#edf2f8;border-radius:5px;overflow:hidden}}.bar-fill{{height:100%;border-radius:5px}}.bar-fill.green{{background:var(--green)}}.bar-fill.red{{background:var(--red)}}.bar-fill.gray{{background:#64748b}}.bar-fill.amber{{background:var(--amber)}}
.table-wrap{{overflow-x:auto}}table{{border-collapse:collapse;width:100%;font-size:12px}}th,td{{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line);vertical-align:top}}th{{color:var(--muted);font-weight:650;background:#f8fafc}}td a{{color:var(--blue);font-weight:700}}.message{{min-width:180px;max-width:360px;overflow-wrap:anywhere}}
.pill{{display:inline-block;border-radius:999px;padding:4px 8px;font-size:10px;font-weight:800;background:#e2e8f0;color:#475569}}.pill.red{{color:#991b1b;background:#fee2e2}}
details summary{{color:var(--blue);cursor:pointer;white-space:nowrap}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;max-height:280px;overflow:auto;background:#f1f5f9;padding:12px;border-radius:8px;font-size:11px;min-width:200px}}
.empty{{color:var(--muted);padding:20px;text-align:center}}.note{{padding:13px 15px;border:1px solid #bfdbfe;background:#eff6ff;color:#1e40af;border-radius:10px;font-size:12px}}.footer{{color:var(--muted);font-size:11px;margin-top:18px}}
@media(max-width:1050px){{aside{{width:175px}}main{{padding:20px}}.metrics{{grid-template-columns:repeat(3,minmax(0,1fr))}}.charts{{grid-template-columns:1fr}}}}
@media(max-width:700px){{aside{{display:none}}main{{padding:14px}}.topbar{{display:block}}.metrics{{grid-template-columns:repeat(2,minmax(0,1fr))}}h1{{font-size:22px}}}}
</style></head><body><div class="layout">
<aside><div class="brand">◈ QA Agent</div><div class="brand-sub">Jenkins Build Dashboard</div>
<a class="nav active" href="#summary">▦ Build özeti</a><a class="nav" href="#history">▤ Geçmiş build'ler</a><a class="nav" href="#charts">▥ Grafikler</a><a class="nav" href="#failures">☑ Başarısız testler</a><a class="nav" href="#console">▤ Konsol kayıtları</a><a class="nav" href="{escaped_url}" target="_blank" rel="noreferrer">↗ Jenkins</a>
<div class="muted" style="color:#c2d0e4;margin-top:40px">Rapor otomatik oluşturuldu.<br>Sonuçlar rapor dosyalarından alınır.</div></aside>
<main>
<header class="topbar" id="summary"><div><h1>Build #{build['number']}</h1><p class="muted">OpenCart Selenium UI Tests · QA build raporu</p>
<div class="meta"><span>Build zamanı: {html.escape(fmt_time(build['timestamp']))}</span><span>Süre: {html.escape(fmt_duration(build['duration_ms']))}</span><span>Suite: {html.escape(s['suite'])}</span></div></div>
<span class="status {status_class}">{html.escape(status_text)} · {html.escape(build['result'])}</span></header>
<section class="metrics">
<div class="metric blue"><span>Toplam test</span><strong>{total}</strong><small>Surefire XML</small></div><div class="metric green"><span>Başarılı</span><strong>{passed}</strong><small>%{passed_pct:.1f}</small></div>
<div class="metric red"><span>Başarısız</span><strong>{failures}</strong><small>%{failed_pct:.1f}</small></div><div class="metric amber"><span>Atlanan</span><strong>{skipped}</strong><small>%{skipped_pct:.1f}</small></div><div class="metric"><span>Hata (Errors)</span><strong>{errors}</strong><small>%{error_pct:.1f}</small></div></section>
<section class="panel"><div class="head"><div><h2>Build bilgisi</h2><p class="muted">Jenkins tarafından bildirilen build durumu</p></div><span class="pill">Build #{build['number']}</span></div>
<div class="meta"><span>Build süresi: {html.escape(fmt_duration(build['duration_ms']))}</span><span>Test süresi: {s['time']:.1f} saniye</span><span>Sonuç: {html.escape(build['result'])}</span></div>
<div class="actions"><a class="btn primary" href="{escaped_url}" target="_blank" rel="noreferrer">Jenkins build'ini aç ↗</a><a class="btn" href="{report_href}">Markdown raporu</a></div></section>
<section class="panel" id="history"><div class="head"><div><h2>Geçmiş build karşılaştırması</h2><p class="muted">Son {len(build_history)} tamamlanmış build · Surefire XML varsa test sayıları gösterilir.</p></div></div>
<div class="table-wrap"><table><thead><tr><th>Build</th><th>Durum</th><th>Zaman</th><th>Süre</th><th>Toplam</th><th>Başarılı</th><th>Başarısız</th><th>Hata</th><th>Atlanan</th></tr></thead><tbody>{''.join(history_rows)}</tbody></table></div>
<p class="muted">“—” işareti, o build için Surefire XML raporunun alınamadığını belirtir; sıfır test anlamına gelmez.</p></section>
<div class="charts" id="charts"><section class="panel"><div class="head"><h2>Test sonuçlarının dağılımı</h2><span class="muted">Surefire</span></div>
<div class="donut-area"><div class="donut"><div class="donut-center"><strong>{total}</strong><span>Toplam test</span></div></div><div class="legend">
<div class="legend-row"><span class="dot" style="background:#16a36a"></span>Başarılı <strong>{passed}</strong></div><div class="legend-row"><span class="dot" style="background:#ef4444"></span>Başarısız <strong>{failures}</strong></div><div class="legend-row"><span class="dot" style="background:#64748b"></span>Hata <strong>{errors}</strong></div><div class="legend-row"><span class="dot" style="background:#f5b82e"></span>Atlanan <strong>{skipped}</strong></div></div></div></section>
<section class="panel"><div class="head"><h2>Sonuç grafiği</h2><span class="muted">Test sayısı</span></div><div class="bars">
<div class="bar-row"><span>Başarılı</span><div class="bar-track"><div class="bar-fill green" style="width:{passed_pct:.2f}%"></div></div><strong>{passed}</strong></div>
<div class="bar-row"><span>Başarısız</span><div class="bar-track"><div class="bar-fill red" style="width:{failed_pct:.2f}%"></div></div><strong>{failures}</strong></div>
<div class="bar-row"><span>Atlanan</span><div class="bar-track"><div class="bar-fill amber" style="width:{skipped_pct:.2f}%"></div></div><strong>{skipped}</strong></div>
<div class="bar-row"><span>Hata</span><div class="bar-track"><div class="bar-fill gray" style="width:{error_pct:.2f}%"></div></div><strong>{errors}</strong></div></div></section></div>
<section class="panel" id="failures"><div class="head"><div><h2>Başarısız testler</h2><p class="muted">Surefire XML kayıtları ve mevcut hata mesajları</p></div><span class="pill red">{len(surefire['failed_tests'])} kayıt</span></div>
<div class="table-wrap"><table><thead><tr><th>Test / Sınıf</th><th>Tür</th><th>Hata mesajı</th><th>Detay</th></tr></thead><tbody>{''.join(fail_rows)}</tbody></table></div></section>
{testng_html}
<section class="panel" id="console"><div class="head"><div><h2>Konsol özetleri</h2><p class="muted">Konsoldaki test özetleri; farklı kapsamlar ayrı gösterilir.</p></div></div>
<div class="table-wrap"><table><thead><tr><th>Satır</th><th>Kapsam</th><th>Test</th><th>Başarısız</th><th>Hata</th><th>Atlanan</th></tr></thead><tbody>{''.join(console_rows)}</tbody></table></div></section>
<div class="note">Bu dashboard build ve test raporlarını özetler; kök neden analizi yapmaz. Test sonuçları tek başına üretim ortamına çıkış onayı anlamına gelmez. Surefire, TestNG ve konsol verileri ayrı kaynaklar olarak sunulur.</div>
<div class="footer">QA Agent · Build #{build['number']} · Rapor oluşturulma zamanı: {datetime.now().astimezone().strftime('%d.%m.%Y %H:%M:%S')}</div>
</main></div></body></html>"""


def main():
    print("===== QA AGENT — BUILD DASHBOARD =====")
    try:
        build = collect_jenkins_data()
        surefire = parse_surefire(build["surefire_path"])
        testng = parse_testng(build["testng_path"])
        summaries = parse_console_summaries(build["console_path"])
    except Exception as exc:
        print(f"Rapor hazırlanamadı: {type(exc).__name__}: {exc}")
        return

    html_path = OUTPUT_DIR / f"qa_dashboard_build_{build['number']}.html"
    md_path = OUTPUT_DIR / f"qa_report_build_{build['number']}.md"
    html_path.write_text(
        render_dashboard(
            build, surefire, testng, summaries, md_path, build["build_history"]
        ),
        encoding="utf-8",
    )
    md_path.write_text(
        build_report_markdown(build, surefire, testng, summaries, html_path),
        encoding="utf-8",
    )

    s = surefire["summary"]
    status, _ = status_label(build["result"])
    print("\n===== KISA BUILD OZETI =====")
    print(f"Build #{build['number']} — {status}")
    print(
        f"Test: {s['tests']} | Başarısız: {s['failures']} | "
        f"Hata: {s['errors']} | Atlanan: {s['skipped']}"
    )
    print(f"Dashboard: {html_path}")
    print(f"Markdown raporu: {md_path}")
    print("Not: Bu sürüm kök neden analizi yapmaz; build özeti ve rapor görünümü üretir.")


if __name__ == "__main__":
    main()

