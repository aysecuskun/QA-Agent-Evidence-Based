
import os
import re
import json
import base64
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from pathlib import Path
from openai import OpenAI


# 1. Ayarlar
JOB_NAME = "OpenCart-Selenium-UI-Tests"

OUTPUT_DIR = Path(__file__).resolve().parent
REPORT_FILENAME = "TEST-TestSuite.xml"
CONSOLE_FILENAME = "jenkins_console.log"


# 2. Jenkins API bağlantısı
def jenkins_request(url):
    username = os.getenv("JENKINS_USER")
    token = os.getenv("JENKINS_API_TOKEN")
    base_url = os.getenv("JENKINS_URL", "http://localhost:8080").rstrip("/")

    if not username or not token:
        raise RuntimeError(
            "JENKINS_USER veya JENKINS_API_TOKEN eksik."
        )

    credentials = base64.b64encode(
        f"{username}:{token}".encode("utf-8")
    ).decode("ascii")

    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {credentials}"}
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()



def collect_jenkins_data():
    base_url = os.getenv(
        "JENKINS_URL", "http://localhost:8080"
    ).rstrip("/")

    job_url = f"{base_url}/job/{JOB_NAME}"

    print("\n===== JENKINS VERI TOPLAMA =====")

    # Son tamamlanmış build'i belirle
    job_data = json.loads(
        jenkins_request(f"{job_url}/api/json")
    )

    last_build = job_data.get("lastCompletedBuild")

    if not last_build:
        raise RuntimeError(
            "Jenkins'te tamamlanmış bir build bulunamadı."
        )

    build_number = last_build["number"]
    build_url = f"{job_url}/{build_number}"

    # Build ayrıntılarını doğrula
    build_data = json.loads(
        jenkins_request(
            f"{build_url}/api/json"
            "?tree=number,result,url,building,timestamp,duration"
        )
    )

    if build_data.get("building") is True:
        raise RuntimeError(
            "Seçilen build hâlâ çalışıyor. Veri toplama durduruldu."
        )

    build_result = build_data.get("result")

    if not build_result:
        raise RuntimeError(
            "Build sonucu belirlenemedi. Veri toplama durduruldu."
        )

    print("Build:", build_number)
    print("Sonuç:", build_result)
    print("Build URL:", build_data.get("url"))

    # Aynı build'in konsol logunu indir
    console_text = jenkins_request(
        f"{build_url}/consoleText"
    ).decode("utf-8", errors="replace")

    console_path = OUTPUT_DIR / (
        f"jenkins_build_{build_number}_{CONSOLE_FILENAME}"
    )
    console_path.write_text(
        console_text, encoding="utf-8"
    )

    # Aynı build'in Surefire XML raporunu indir
    report_url = (
        f"{build_url}/artifact/target/surefire-reports/"
        f"{REPORT_FILENAME}"
    )

    report_content = jenkins_request(report_url)

    report_path = OUTPUT_DIR / (
        f"jenkins_build_{build_number}_{REPORT_FILENAME}"
    )
    report_path.write_bytes(report_content)

    # Aynı build'in TestNG XML raporunu indir
    testng_report_url = (
        f"{build_url}/artifact/target/surefire-reports/"
        "testng-results.xml"
    )

    testng_report_content = jenkins_request(
        testng_report_url
    )

    testng_report_path = OUTPUT_DIR / (
        f"jenkins_build_{build_number}_testng-results.xml"
    )
    testng_report_path.write_bytes(testng_report_content)

    print("Konsol dosyası:", console_path)
    print("Surefire XML raporu:", report_path)
    print("TestNG XML raporu:", testng_report_path)

    return {
        "build_number": build_number,
        "build_result": build_result,
        "build_url": build_data.get("url", build_url),
        "console_path": console_path,
        "report_path": report_path,
        "testng_report_path": testng_report_path,
    }




# 3. XML test raporunu oku
def read_test_report(report_path):
    if not report_path.exists():
        print(f"Rapor bulunamadı: {report_path}")
        return None

    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, OSError) as exc:
        print(f"XML raporu okunamadı: {exc}")
        return None

    try:
        summary = {
            "suite": root.attrib.get("name", "Bilinmiyor"),
            "tests": int(root.attrib.get("tests", "0")),
            "failures": int(root.attrib.get("failures", "0")),
            "errors": int(root.attrib.get("errors", "0")),
            "skipped": int(root.attrib.get("skipped", "0")),
        }
    except ValueError as exc:
        print(f"XML test sayıları geçersiz: {exc}")
        return None

    failed_tests = []

    for test in root.findall(".//testcase"):
        failure = test.find("failure")
        error = test.find("error")

        if failure is not None or error is not None:
            issue = failure if failure is not None else error

            failed_tests.append({
                "test": test.attrib.get("name", "Bilinmiyor"),
                "class": test.attrib.get("classname", "Bilinmiyor"),
                "error_type": issue.tag,
                "message": issue.attrib.get("message", "Mesaj yok"),
                "details": (issue.text or "")[:3000],
            })

    return {
        "summary": summary,
        "failed_tests": failed_tests,
    }



def read_testng_report(report_path):
    if not report_path.exists():
        print(f"TestNG raporu bulunamadı: {report_path}")
        return None

    try:
        root = ET.parse(report_path).getroot()
    except (ET.ParseError, OSError) as exc:
        print(f"TestNG XML raporu okunamadı: {exc}")
        return None

    try:
        summary = {
            "total": int(root.attrib.get("total", "0")),
            "passed": int(root.attrib.get("passed", "0")),
            "failed": int(root.attrib.get("failed", "0")),
            "skipped": int(root.attrib.get("skipped", "0")),
            "ignored": int(root.attrib.get("ignored", "0")),
        }
    except ValueError as exc:
        print(f"TestNG XML sayıları geçersiz: {exc}")
        return None

    failed_tests = []

    # TestNG XML'de sınıf adı test-method üzerinde değil, üstteki <class> etiketindedir.
    # Bu nedenle her class düğümünün altındaki metotları o sınıf adıyla ilişkilendiriyoruz.
    for class_element in root.findall(".//class"):
        class_name = class_element.attrib.get("name", "Bilinmiyor")

        for test in class_element.findall(".//test-method"):
            if test.attrib.get("status", "").upper() == "FAIL":
                failed_tests.append({
                    "name": test.attrib.get("name", "Bilinmiyor"),
                    "class": class_name,
                    "signature": test.attrib.get("signature", ""),
                    "is_config": test.attrib.get("is-config", "false").lower() == "true",
                })

    return {
        "summary": summary,
        "failed_tests": failed_tests,
    }


# 4. Konsol logunu oku
def read_console_log(console_path, build_result):
    if not console_path.exists():
        print(f"Konsol dosyası bulunamadı: {console_path}")
        return None

    content = console_path.read_text(
        encoding="utf-8",
        errors="replace"
    )

    lines = content.splitlines()
    test_summaries = []

    summary_pattern = re.compile(
        r"Tests run:\s*(\d+),\s*"
        r"Failures:\s*(\d+),\s*"
        r"Errors:\s*(\d+),\s*"
        r"Skipped:\s*(\d+)",
        re.IGNORECASE
    )

    for line_number, line in enumerate(lines, start=1):
        match = summary_pattern.search(line)

        if match:
            test_summaries.append({
                "line": line_number,
                "tests": int(match.group(1)),
                "failures": int(match.group(2)),
                "errors": int(match.group(3)),
                "skipped": int(match.group(4)),
            })

    keywords = [
        "[error]", "exception", "failed",
        "failure", "build failure",
    ]

    error_lines = [
        f"Satır {number}: {line}"
        for number, line in enumerate(lines, start=1)
        if any(keyword in line.lower() for keyword in keywords)
    ]

    return {
        "build_result": build_result,
        "summaries": test_summaries,
        "error_lines": error_lines[:80],
    }


# 5. Kaynaklar arasındaki tutarlılığı kontrol et
def validate_build_data(test_report, console_data, testng_report):
    xml_summary = test_report["summary"]
    console_summaries = console_data["summaries"]
    testng_summary = testng_report["summary"]

    print("\n===== VERİ TUTARLILIK KONTROLÜ =====")
    print("Build sonucu:", console_data["build_result"])
    print("Surefire XML özeti:", xml_summary)
    print("TestNG XML özeti:", testng_summary)

    if console_data["build_result"] == "UNKNOWN":
        print("Build sonucu bilinmiyor. Analiz durduruldu.")
        return False

    if not console_summaries:
        print("Konsolda test özeti yok. Analiz durduruldu.")
        return False

    # TestNG raporunun kendi toplamlarının aritmetik tutarlılığını kontrol et.
    testng_calculated_total = (
        testng_summary["passed"]
        + testng_summary["failed"]
        + testng_summary["skipped"]
        + testng_summary["ignored"]
    )

    print(
        "TestNG toplam kontrolü:",
        f"total={testng_summary['total']},",
        f"durumların toplamı={testng_calculated_total}",
    )

    if testng_summary["total"] != testng_calculated_total:
        print("TestNG raporunun kendi sayıları tutarsız. Analiz durduruldu.")
        return False

    # Başarısız test kayıtlarını karşılaştırma için görünür kıl.
    print("\nSurefire XML başarısız kayıtları:")
    if test_report["failed_tests"]:
        for item in test_report["failed_tests"]:
            print(
                f"- {item['class']}.{item['test']}: "
                f"{item['message']}"
            )
    else:
        print("- Başarısız kayıt bulunamadı.")

    print("\nTestNG XML başarısız metotları:")
    if testng_report["failed_tests"]:
        for item in testng_report["failed_tests"]:
            config_label = " (configuration metodu)" if item["is_config"] else ""
            print(f"- {item['class']}.{item['name']}{config_label}")
    else:
        print("- Başarısız metot bulunamadı.")

    # Birden fazla konsol özeti varsa kapsamları netleşmeden birini seçme.
    if len(console_summaries) > 1:
        print(
            f"\\nKonsolda {len(console_summaries)} test özeti bulundu."
        )

        for index, item in enumerate(console_summaries, start=1):
            print(
                f"Özet {index}, satır {item['line']}: "
                f"tests={item['tests']}, "
                f"failures={item['failures']}, "
                f"errors={item['errors']}, "
                f"skipped={item['skipped']}"
            )

        print(
            "Özetlerin kapsamı netleştirilmeden analiz yapılmayacak."
        )
        return False

    console_summary = console_summaries[0]
    fields = ["tests", "failures", "errors", "skipped"]
    mismatches = []

    print("\n===== SUREFIRE XML / KONSOL SAYISAL KARŞILAŞTIRMASI =====")

    for field in fields:
        xml_value = xml_summary[field]
        console_value = console_summary[field]

        print(f"{field}: XML={xml_value}, Konsol={console_value}")

        if xml_value != console_value:
            mismatches.append(field)

    if mismatches:
        print("Veri tutarsızlığı bulundu:", ", ".join(mismatches))
        print("OpenAI analizi durduruldu.")
        return False

    print(
        "Surefire XML ile tek konsol özeti sayısal olarak uyumlu."
    )
    print(
        "TestNG sayıları Surefire ile doğrudan eşit olmak zorunda "
        "kabul edilmiyor; raporlar farklı türde test/configuration "
        "kayıtlarını sayabilir. Ayrışan sonuçlar yukarıda gösterildi."
    )
    print(
        "Uyarı: Sayısal uyum, kaynakların aynı build'e ait olduğunu "
        "tek başına kanıtlamaz."
    )

    return True


# 6. OpenAI analizi
def analyze_with_openai(test_report, console_data, testng_report, build_info):
    client = OpenAI()

    prompt = f"""
Bir QA Automation Engineer gibi davran ve aşağıdaki verileri Türkçe analiz et.

Kurallar:
- Kanıtlanmış bilgileri, hipotezleri ve bilinmeyenleri ayır.
- Kanıt olmadan kesin kök neden ileri sürme.
- Test kodu görülmeden kesin kod değişikliği önerme.
- Hata mesajlarına dayanarak doğrulama adımları öner.
- Veride olmayan ayrıntıları uydurma.
- Build ve test sonuçlarını birbirine karıştırma.

Build bilgileri:
{build_info}

XML test raporu:
{json.dumps(test_report, ensure_ascii=False, indent=2)}

Konsol verileri:
{json.dumps(console_data, ensure_ascii=False, indent=2)}

TestNG XML raporu:
{json.dumps(testng_report, ensure_ascii=False, indent=2)}

Rapor bölümleri:
1. Genel durum
2. XML ve konsol karşılaştırması
3. Başarısız testler ve kanıtlar
4. Olası nedenler ve bunları doğrulama adımları
5. Öncelikli sonraki adımlar
"""

    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
    )

    return response.choices[0].message.content


# 7. Ana akış
def main():
    print("===== QA AGENT BAŞLATILDI =====")

    try:
        build_info = collect_jenkins_data()
    except Exception as exc:
        print(
            f"Jenkins veri toplama hatası: "
            f"{type(exc).__name__}: {exc}"
        )
        print("İşlem güvenli şekilde durduruldu.")
        return

    test_report = read_test_report(build_info["report_path"])
    testng_report = read_testng_report(build_info["testng_report_path"])
    console_data = read_console_log(
        build_info["console_path"],
        build_info["build_result"],
    )

    if (
        test_report is None
        or testng_report is None
        or console_data is None
    ):
        print("Gerekli raporlardan biri okunamadı. Analiz yapılmayacak.")
        return

    print("\n===== SUREFIRE XML TEST RAPORU =====")
    print(test_report["summary"])
    print("Surefire başarısız kayıt sayısı:", len(test_report["failed_tests"]))

    print("\n===== TESTNG XML TEST RAPORU =====")
    print(testng_report["summary"])
    print("TestNG başarısız metot kaydı sayısı:", len(testng_report["failed_tests"]))

    if not validate_build_data(test_report, console_data, testng_report):
        print("Veriler doğrulanamadı. OpenAI çağrısı yapılmadı.")
        return

    print("\n===== OPENAI ANALİZİ =====")

    try:
        analysis = analyze_with_openai(
            test_report,
            console_data,
            testng_report,
            {
                "build_number": build_info["build_number"],
                "build_result": build_info["build_result"],
                "build_url": build_info["build_url"],
            },
        )
        print(analysis)

    except Exception as exc:
        print(
            f"OpenAI analiz hatası: "
            f"{type(exc).__name__}: {exc}"
        )


if __name__ == "__main__":
    main()
