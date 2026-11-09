#!/usr/bin/env python3
# build.py
# Forge FFmpeg (V15): kilitli kaynaklardan FFmpegKitNext + FFmpeg derlemesi, kapalı başarısız denetimler, paketleme.
# Yalnız standart kütüphane (Python 3.9+). Hiçbir yerde `assert` yok: -O ve PYTHONOPTIMIZE=1 altında da
# aynı çıkış kodları çıkar. Çıkış kodları: 0 başarılı, 1 kırmızı (denetim ya da derleme hatası), 2 kullanım hatası.
"""Kullanım:
  python3 build.py all --work <yeni dizin> --out <yeni dizin> --label <v9.0.2-forge.N | deneme-xxxxxxx>
  python3 build.py selftest
  python3 build.py lock-durum

Aşamalar (all hepsini bu sırayla koşar): tools, fetch, verify-lock (önce), archive, patch, build,
verify-lock (sonra), check, package. Ayrı ayrı da koşulabilirler; `--help` ile her birinin girdisi görülür.
`tani --work <dizin> --out <yeni dizin>` aşama zincirinde değildir: başarısız ya da iptal koşunun günlüklerini sınırlı ve gizlenmiş toplar.
"""
import argparse
import ast
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.realpath(__file__))

# MARK: - Sabitler

NEXT_ADI = "ffmpeg-kit-next"
NEXT_REPO = "https://github.com/arthenica/ffmpeg-kit-next"
# Kilitteki on beş kaynak ve Next'in beklediği yerleşim: Next kökte, bileşenler src/<ad>, gnu-config .tmp/source/config,
# gas-preprocessor .tmp/source/gas-preprocessor (betiği build.py .tmp/ içine yerleştirir, aşağıda).
# dlg FreeType'ın gitlink'idir (SK-936-2): yeri aşağıdaki sabit tabloda, src/ altında kendi dizini yoktur.
KAYNAK_ADLARI = ["ffmpeg-kit-next", "ffmpeg", "expat", "fontconfig", "freetype", "fribidi", "harfbuzz",
                 "lame", "libass", "libpng", "libvpx", "opus", "config", "gas-preprocessor", "dlg"]
CONFIG_ADI = "config"
GAS_ADI = "gas-preprocessor"
# Next'in kaynak ağacı içinde değil .tmp/source/<ad> altında duran kilitli kaynaklar.
TMP_KAYNAKLARI = (CONFIG_ADI, GAS_ADI)
# SHA-256 anahtarı yalnız GAS_ADI girdisinde bulunur (kilitli blob'un özeti); başka girdide bulunması da, GAS_ADI'da olmaması da kırmızıdır.
KAYNAK_ANAHTARLARI = frozenset(["name", "url", "ref", "version", "commit"])
GAS_ANAHTARLARI = KAYNAK_ANAHTARLARI | frozenset(["sha256"])
# İç içe kaynak -> (üst ağaç, üst ağaçtaki yol). Kapalı tablodur: lock.json'dan ya da argümandan istisna gelmez (SK-936-1).
IC_KAYNAKLAR = {"dlg": ("freetype", "subprojects/dlg")}
# İ2: kökteki .gitmodules için izinli blob SHA'ları. Boş blob yalnız hiç gitlink olmayan ağaçta, FreeType'ınki yalnız İ1 sağlanan
# freetype ağacında geçerlidir (içerik: [submodule "dlg"] path = subprojects/dlg url = https://github.com/nyorain/dlg.git).
GITMODULES_BOS = b"e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
GITMODULES_FREETYPE = b"b452dffb93b18b41c75a50de9c08e4b07962ee6e"

XCODE_UYGULAMA = "/Applications/Xcode_26.6.app"
XCODE_BUILD = "17F113"

# Next'e verilen seçenekler (adlar Next v9.0.2 `process_common_build_option`/`ios.sh` kaynağından okundu; derlemede çalıştırılmadı).
# --arch listesi varsayılan mimari kümesini (arm64e, x86-64, mac-catalyst) siler; yalnız iki dilim kalır.
IOS_SECENEKLERI = [
    "--xcframework", "--target=16.1", "--no-bitcode", "--no-ffmpeg-kit-protocols",
    "--arch=arm64,arm64-simulator",
    "--enable-lib-ios-audiotoolbox", "--enable-lib-ios-avfoundation", "--enable-lib-ios-videotoolbox",
    "--enable-lib-ios-zlib", "--enable-lib-ios-bzip2", "--enable-lib-ios-libiconv",
    "--enable-lib-libvpx", "--enable-lib-opus", "--enable-lib-libass", "--enable-lib-fontconfig",
    "--enable-lib-freetype", "--enable-lib-fribidi", "--enable-lib-lame",
    # Ölçülen tarifteki lame yeniden yapılandırması korunur.
    "--reconf-lib-lame",
    # Next, freetype'ı etkinleştirince libjxl'i de açar (function.sh set_library freetype); bu bizim kümemizde yok.
    # Bayrak libjxl'i ve ondan gelen indirmeyi kapatır; kilitte olmayan bir src/ dizini ayrıca verify-lock'ta kırmızı olur.
    "--disable-lib-libjxl",
]

# Derlemede PATH'e eklenen keg-only Homebrew formülleri (gettext, bison, texinfo); GNU sed ayrıca SED ile verilir (Next `prepare_inline_sed` GNU sed ister).
BREW_YOL_FORMULLERI = ["gettext", "bison", "texinfo"]
GEREKLI_ARACLAR = ["git", "otool", "make", "autoconf", "automake", "pkg-config", "cmake", "meson", "ninja", "nasm"]
TEMEL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Derleme ilerleme bekçisi (zamanli_calistir): ios.sh bu kadar saniye hiç stdout vermezse öldürülür. Her kütüphane `ad: ` ile başlar ve
# `ok` ile biter; ilk deneme koşusunda libpng/expat sonrası 3 sa 53 dk sessizlik oldu. Kütüphane başına süre ölçülmedi (kütüphaneler
# ilk koşuda 15-40 sn sürdü; ffmpeg ve xcframework aşamalarının süresi bilinmiyor): çok kısaltılırsa sağlıklı koşuyu öldürebilir.
SESSIZLIK_SINIRI = 40 * 60
LOG_SONU_BAYT = 256 * 1024

# Derleme ortamının izin listesi. PKG_CONFIG_PATH tanımsız kalır: Next main-ios.sh PKG_CONFIG_LIBDIR'i kendi kurulum dizinine bağlar ve
# Homebrew'un .pc dosyalarının (brotli gibi kilitte olmayan kitaplıklar) görünmesi derlemeyi kilitten bağımsız kılar.
ENV_IZINLI = frozenset(["PATH", "HOME", "TMPDIR", "LANG", "GIT_TERMINAL_PROMPT", "DEVELOPER_DIR", "ACLOCAL_PATH", "SED"])

CERCEVELER = ["ffmpegkit", "libavcodec", "libavdevice", "libavfilter", "libavformat", "libavutil",
              "libswresample", "libswscale"]
DILIMLER = ("ios-arm64", "ios-arm64-simulator")

# configure dizesinde tam belirteç olarak bulunması gereken beş güvenlik bayrağı (bu sırayla yamalanır).
GUVENLIK_BAYRAKLARI = ("--disable-network", "--disable-protocols", "--enable-protocol=file",
                       "--disable-demuxer=hls,dash,concat,image2", "--disable-muxer=tee")
YASAK_ENABLE = ("--enable-protocols", "--enable-network")
YASAK_ONEKLER = ("--enable-demuxer", "--enable-muxer")
GPL_BAYRAKLARI = ("--enable-gpl", "--enable-nonfree", "--enable-libx264", "--enable-libx265", "--enable-libxvid",
                  "--enable-libvidstab", "--enable-librubberband")
# Bugünkü harici kütüphane kümesi (libass'ın ve fontconfig'in bağımlılıkları dahil); configure'da bundan fazlası ya da eksiği kırmızıdır.
HARICI_KITAPLIKLAR = ("--enable-libvpx", "--enable-libopus", "--enable-libass", "--enable-libfontconfig", "--enable-libfreetype",
                      "--enable-libfribidi", "--enable-libharfbuzz", "--enable-libmp3lame")
ZORUNLU_BAYRAKLAR = ("--enable-version3", "--enable-videotoolbox") + HARICI_KITAPLIKLAR
YASAK_DEMUXER = ("hls", "dash", "concat", "image2")

# Silinen dosyalar (T-10): GPL metni taşıyabilen betik ve yabancı bağlantı gösteren SOURCE.
PAKETTEN_SILINEN = ("strip-frameworks.sh", "SOURCE")

# Forge'daki biçim 2 bayt sözleşmesi (ffmpeg_pin.py parse_manifest / parse_list(fmt=2)).
MANIFEST_KIND = "vendored-source-build"
LISTE_BASLIK = ["# forge-ffmpeg-pin 2", "# kind: " + MANIFEST_KIND]
LISTE_SON_BASLIK = "# authoritative manifest: lgpl-manifest.json (committed source-built binaries)"

ETIKET_RE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+-forge\.[0-9]+")
DENEME_RE = re.compile(r"deneme-[0-9a-f]{7}")
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
YOL_KARAKTER_RE = re.compile(r"[\x21-\x5b\x5d-\x7e]+")   # yazdırılabilir ASCII; boşluk, ters bölü ve ASCII dışı yok
DIST_AD_RE = re.compile(r"[A-Za-z0-9._-]+")
TARIH_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

GIT_SABIT = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", "-c", "core.quotepath=false",
             "-c", "advice.detachedHead=false", "-c", "gc.auto=0", "-c", "core.fsmonitor=false"]

ZIP_TOPLAM_SINIR = 2 * 1024 * 1024 * 1024
ZIP_DOSYA_SINIR = 512 * 1024 * 1024
WINDOWS_AYRILMIS = set(["con", "prn", "aux", "nul"] + ["com%d" % i for i in range(1, 10)] + ["lpt%d" % i for i in range(1, 10)])


class Kirmizi(Exception):
    """Bir denetim ya da adım başarısız; main çıkış kodu 1 ile sonlandırır."""


def kirmizi_if(kosul, mesaj):
    if kosul:
        raise Kirmizi(mesaj)


# MARK: - Günlük gizleme

# Hata mesajına ve tanı artifact'ına giren metin önce buradan geçer. Derleme ortamı izin listesiyle sınırlı olduğundan (ENV_IZINLI)
# beklenen içerik zaten gizlisizdir; bu ikinci savunmadır (GitHub adım günlüğünü maskeler, artifact'ı maskelemez).
GIZLE_KALIPLARI = (
    (re.compile(r"://[^/@\s]+@"), "://***@"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "***"),
    (re.compile(r"(?im)^([ \t]*(?:proxy-)?authorization[ \t]*:).*$"), r"\1 ***"),
    (re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9._~+/=-]{8,}"), "bearer ***"),
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|CREDENTIAL|AUTH)[A-Za-z0-9_]*)=[^\r\n]*"), r"\1=***"),
)


def gizle(metin):
    for kalip, yerine in GIZLE_KALIPLARI:
        metin = kalip.sub(yerine, metin)
    return metin


def git_hata_ozeti(err, sinir=300):
    """git stderr'inin son üç boş olmayan satırı: gizlenmiş, kısaltılmış; boşsa 'çıktı yok'."""
    satirlar = [l.strip() for l in gizle(err.decode("utf-8", "replace")).splitlines() if l.strip()][-3:]
    return " | ".join(satirlar)[:sinir] or "çıktı yok"


# MARK: - git yardımcıları

def git(args, cwd=None, dosya_izni=False, girdi=None, zaman=900):
    komut = ["git"] + GIT_SABIT
    if dosya_izni:
        komut += ["-c", "protocol.file.allow=always"]
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        p = subprocess.run(komut + list(args), cwd=cwd, env=env, input=girdi,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=zaman)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Kirmizi("git çalıştırılamadı: %s" % type(e).__name__)
    return p.returncode, p.stdout, p.stderr


def git_ok(args, cwd=None, dosya_izni=False, girdi=None):
    rc, out, err = git(args, cwd, dosya_izni, girdi)
    if rc != 0:
        ozet = gizle(err.decode("utf-8", "replace")).strip().splitlines()[-1:] or [""]
        raise Kirmizi("git %s başarısız (kod %d): %s" % (args[0], rc, ozet[0][:200]))
    return out


def git_metin(args, cwd=None):
    return git_ok(args, cwd).decode("utf-8", "replace").strip()


# MARK: - Kilit

def _yinelenen_anahtar_yok(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError("yinelenen anahtar")
        d[k] = v
    return d


def kilit_oku(yol, siki=True, dosya_izni=False):
    """lock.json'u çözer ve biçimini denetler. siki=True iken commit'i olmayan (doğrulanmadı) kaynak da kırmızıdır."""
    try:
        with open(yol, "rb") as f:
            veri = f.read()
        nesne = json.loads(veri.decode("utf-8"), object_pairs_hook=_yinelenen_anahtar_yok)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise Kirmizi("lock.json okunamadı: JSON değil, UTF-8 değil ya da yinelenen anahtar var")
    kirmizi_if(not isinstance(nesne, dict) or set(nesne) != set(["schema", "sources"]), "lock.json anahtarları schema ve sources olmalı")
    kirmizi_if(type(nesne["schema"]) is not int or nesne["schema"] != 1, "lock.json schema 1 değil")
    kaynaklar = nesne["sources"]
    kirmizi_if(not isinstance(kaynaklar, list), "sources liste değil")
    adlar = []
    eksik = []
    for k in kaynaklar:
        kirmizi_if(not isinstance(k, dict) or set(k) != (GAS_ANAHTARLARI if k.get("name") == GAS_ADI else KAYNAK_ANAHTARLARI),
                   "kaynak girdisi name, url, ref, version, commit anahtarlarını taşımalı (sha256 yalnız %s girdisinde, orada zorunlu)" % GAS_ADI)
        for alan in ("name", "url", "ref", "version"):
            kirmizi_if(not isinstance(k[alan], str) or not k[alan], "%s alanı boş ya da dize değil" % alan)
        ad = k["name"]
        kirmizi_if(ad in adlar, "yinelenen kaynak: %s" % ad)
        adlar.append(ad)
        url_ok = URL_RE.fullmatch(k["url"]) is not None and not k["url"].endswith(".git")
        if dosya_izni and k["url"].startswith("file:///"):
            url_ok = True
        kirmizi_if(not url_ok, "%s: url https://github.com/<sahip>/<repo> biçiminde değil" % ad)
        kirmizi_if(REF_RE.fullmatch(k["ref"]) is None, "%s: ref biçimi geçersiz" % ad)
        c = k["commit"]
        if c is None:
            eksik.append(ad)
        else:
            kirmizi_if(not isinstance(c, str) or COMMIT_RE.fullmatch(c) is None, "%s: commit 40 küçük hex değil" % ad)
        if ad == GAS_ADI:
            kirmizi_if(not isinstance(k["sha256"], str) or SHA256_RE.fullmatch(k["sha256"]) is None, "%s: sha256 64 küçük hex değil" % ad)
    kirmizi_if(sorted(adlar) != sorted(KAYNAK_ADLARI), "kaynak kümesi beklenen on beş ad değil (eksik ya da fazla)")
    if siki:
        kirmizi_if(eksik, "commit'i doğrulanmamış kaynak var (lock.json'a doğrulanmış commit yazılmadan derlenmez): %s" % ", ".join(eksik))
    return {"sources": kaynaklar, "unverified": eksik, "bytes": veri}


def kaynak_haritasi(kilit):
    return dict((k["name"], k) for k in kilit["sources"])


def agac_yolu(work, ad):
    if ad == NEXT_ADI:
        return work
    if ad in TMP_KAYNAKLARI:
        return os.path.join(work, ".tmp", "source", ad)
    if ad in IC_KAYNAKLAR:
        ust, goreli = IC_KAYNAKLAR[ad]
        return os.path.join(agac_yolu(work, ust), *goreli.split("/"))
    return os.path.join(work, "src", ad)


# MARK: - fetch ve verify-lock

def kaynak_cek(k, yol, dosya_izni):
    """Tek kaynağı commit SHA'sıyla alır; başarısızsa etikete ya da dala geri dönülmez (SK-893-9).
    İç içe kaynak (SK-936-2): etiketi yoktur, etiket adımı atlanır; hedef dizini üst ağacın checkout'u boş bırakır."""
    ic = k["name"] in IC_KAYNAKLAR
    if ic and os.path.lexists(yol):
        kirmizi_if(os.path.islink(yol) or not os.path.isdir(yol) or os.listdir(yol), "%s: hedef dizin boş değil" % k["name"])
    else:
        os.makedirs(yol)
    git_ok(["init", "-q"], yol)
    rc, _, err = git(["fetch", "--depth", "1", "--no-tags", k["url"], k["commit"]], yol, dosya_izni)
    kirmizi_if(rc != 0, "%s: commit %s indirilemedi; etikete dönülmez (git: %s)" % (k["name"], k["commit"][:12], git_hata_ozeti(err)))
    git_ok(["checkout", "-q", "--detach", "FETCH_HEAD"], yol)
    kirmizi_if(git_metin(["rev-parse", "HEAD"], yol) != k["commit"], "%s: HEAD kilitteki commit değil" % k["name"])
    if ic:
        return
    # Etiket ayrıca getirilir: FFmpeg sürüm dizesi `git describe` ile üretilir ve Next yerel etiket ister.
    rc, _, err = git(["fetch", "--depth", "1", "--no-tags", k["url"], "+refs/tags/%s:refs/tags/%s" % (k["ref"], k["ref"])], yol, dosya_izni)
    kirmizi_if(rc != 0, "%s: ref etiketi getirilemedi (git: %s)" % (k["name"], git_hata_ozeti(err)))
    rc, out, _ = git(["rev-parse", "--verify", "-q", "refs/tags/%s^{commit}" % k["ref"]], yol)
    kirmizi_if(rc != 0 or out.decode().strip() != k["commit"], "%s: ref etiketi kilitteki commit'e işaret etmiyor" % k["name"])


def fetch_sirasi(kilit):
    """İç içe kaynaklar, lock.json'daki sıradan bağımsız olarak üst ağaçlarından sonra çekilir (üst checkout'u hedef dizini açar)."""
    return sorted(kilit["sources"], key=lambda k: k["name"] in IC_KAYNAKLAR)


def fetch(work, kilit, dosya_izni=False):
    kirmizi_if(os.path.lexists(work), "çalışma dizini zaten var: yeni bir dizin verilmeli")
    for k in fetch_sirasi(kilit):
        kaynak_cek(k, agac_yolu(work, k["name"]), dosya_izni)
        print("fetch: %s %s %s" % (k["name"], k["ref"], k["commit"][:12]))
    gas_yerlestir(work, kilit)


# MARK: - gas-preprocessor.pl

# Next, arm64 kütüphanelerinin `AS` değişkenine .tmp/gas-preprocessor.pl'i koyar (FFmpeg'in aarch64 .S dosyaları bu Perl betiğinden geçer) ve
# dosya yoksa onu kilitsiz bir etiket URL'sinden indirir (function-ios.sh set_toolchain_paths). Bu yüzden betik kilitlenir, ios.sh'tan ÖNCE
# yerleştirilir ve Next'in indirme dalı yamayla kapatılır (patches/08). Yerleştirme `sonra` kipinde de bayt bayt aynı doğrulanır.
GAS_DOSYASI = "gas-preprocessor.pl"
# Next'in indirme dalındaki `sed -i 's/s+({/s+(\\{/g;s/s\*})/s*\\})/g'` komutunun eşdeğeri (Perl 5.32'de kaçışsız { düzenli ifadede ölümcül).
# İki parça da düz metindir (BRE'de { ve } özel değil, \* kaçışlı); sed /g'dir, ama gerçek betikte her biri tam bir kez geçer ve sabitlenir.
# Dönüşüm yerleştirme anındadır (ios.sh'tan önce): ios.sh sırasında .tmp/'ye yazan bir yama bu güvencenin gevşetilmesi olurdu.
GAS_DONUSUMLERI = ((b"s+({", b"s+(\\{"), (b"s*})", b"s*\\})"))
# Kilitli blob (lock.json sha256) + yukarıdaki dönüşüm = yerleştirilen baytlar. 2026-11-09'da gerçek blob'a hem GNU sed 4.9 hem bu işlevle
# uygulanıp aynı çıktı alındı; runner'da ölçülmedi.
GAS_SON_SHA256 = "ed085dd43ed95759a2b84afc3b1f9b0b64114b586d0d4b16fbe164b4369ae844"

# .tmp/ üst düzey girdileri: kapalı sabit tablo (SK-951-1); önek ya da joker yok, nokta ile başlayan adlar da sayılır. Değer beklenen türdür.
# Derleme öncesi yalnız {source, gas-preprocessor.pl}. Derleme sonrası Next'in kendi yazdıkları da eklenir (Next @9f04bf1e kaynağından okundu,
# runner'da ölçülmedi): cmake (harfbuzz'ın meson kurulumu, function.sh get_cmake_build_directory) ve --xcframework dalının iki bundle dizini
# (function-ios.sh). mac-catalyst ve düz ios bundle'ı ile ffkitprotocols bu derlemede oluşmaz (--arch iki dilim, --no-ffmpeg-kit-protocols),
# bu yüzden listede yoktur ve çıkarlarsa kırmızıdır. İçlerine inilmez (SK-951-7): download() yalnız .tmp/ üst düzeyine yazar.
TUR_DIZIN, TUR_DOSYA = "dizin", "dosya"
# Bundle adlarındaki sürüm Next'in IOS_MIN_VERSION'ıdır (--target=); ayrı bir sabit yoktur (SK-951-4). Tam bir --target= yoksa yükleme patlar.
(IOS_HEDEF_SURUMU,) = [a.split("=", 1)[1] for a in IOS_SECENEKLERI if a.startswith("--target=")]
TMP_UST_ONCE = {"source": TUR_DIZIN, GAS_DOSYASI: TUR_DOSYA}
TMP_UST_SONRA = dict(TMP_UST_ONCE, **{
    "cmake": TUR_DIZIN,
    "bundle-apple-universal-iphoneos-" + IOS_HEDEF_SURUMU: TUR_DIZIN,
    "bundle-apple-universal-iphonesimulator-" + IOS_HEDEF_SURUMU: TUR_DIZIN,
})


def gas_donustur(veri):
    for eski, _ in GAS_DONUSUMLERI:
        kirmizi_if(veri.count(eski) != 1, "%s: %r tam bir kez bulunmalı (%d bulundu); dönüşüm uygulanmadı" % (GAS_DOSYASI, eski, veri.count(eski)))
    for eski, yeni in GAS_DONUSUMLERI:
        veri = veri.replace(eski, yeni)
    return veri


def gas_yerlestir(work, kilit):
    """Kilitli commit'in nesnesinden (çalışma ağacından değil) betiği okur, kilitteki blob SHA-256'sını denetler, dönüşümü uygular, son
    SHA-256'yı sabitle karşılaştırır ve work/.tmp/gas-preprocessor.pl olarak yazar (0755, yeni normal dosya; var olan yol ya da bağ kırmızı)."""
    k = kaynak_haritasi(kilit)[GAS_ADI]
    yol = agac_yolu(work, GAS_ADI)
    out = git_ok(["ls-tree", "-z", k["commit"], "--", GAS_DOSYASI], yol)
    kayitlar = [r for r in out.split(b"\0") if r]
    kirmizi_if(len(kayitlar) != 1, "%s: kilitli commit'te %s bulunamadı" % (GAS_ADI, GAS_DOSYASI))
    bilgi, _, ad_b = kayitlar[0].partition(b"\t")
    mod, tip, sha = bilgi.split(b" ")
    kirmizi_if(ad_b != GAS_DOSYASI.encode() or tip != b"blob" or mod not in (b"100644", b"100755"), "%s: %s normal dosya değil" % (GAS_ADI, GAS_DOSYASI))
    ham = git_ok(["cat-file", "blob", sha.decode("ascii")], yol)
    kirmizi_if(hashlib.sha256(ham).hexdigest() != k["sha256"], "%s: blob SHA-256'sı lock.json'daki değil" % GAS_DOSYASI)
    son = gas_donustur(ham)
    kirmizi_if(hashlib.sha256(son).hexdigest() != GAS_SON_SHA256, "%s: dönüşümden sonraki SHA-256 beklenen sabit değil" % GAS_DOSYASI)
    tmp = os.path.join(work, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    hedef = os.path.join(tmp, GAS_DOSYASI)
    try:
        fd = os.open(hedef, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o755)
    except OSError:
        raise Kirmizi("%s zaten var ya da yazılamadı" % os.path.relpath(hedef, work).replace(os.sep, "/"))
    with os.fdopen(fd, "wb") as f:
        f.write(son)
    os.chmod(hedef, 0o755)
    sorun = gas_dosyasi_sorunlari(work)
    kirmizi_if(sorun, "yerleştirilen %s: %s" % (GAS_DOSYASI, "; ".join(sorun)))
    print("fetch: %s yerleştirildi (sha256 %s)" % (GAS_DOSYASI, GAS_SON_SHA256[:12]))


def gas_dosyasi_sorunlari(work):
    """work/.tmp/gas-preprocessor.pl yerinde, normal dosya (bağ değil), 0755 (POSIX'te) ve baytları GAS_SON_SHA256 mi? verify-lock her iki kipte koşar."""
    hedef = os.path.join(work, ".tmp", GAS_DOSYASI)
    try:
        st = os.lstat(hedef)
    except OSError:
        return [".tmp/%s yok" % GAS_DOSYASI]
    if not stat.S_ISREG(st.st_mode):
        return [".tmp/%s normal dosya değil (bağ ya da dizin)" % GAS_DOSYASI]
    sorun = []
    if os.name == "posix" and stat.S_IMODE(st.st_mode) != 0o755:
        sorun.append(".tmp/%s kipi 0755 değil" % GAS_DOSYASI)
    try:
        with open(hedef, "rb") as f:
            veri = f.read()
    except OSError:
        return sorun + [".tmp/%s okunamadı" % GAS_DOSYASI]
    if hashlib.sha256(veri).hexdigest() != GAS_SON_SHA256:
        sorun.append(".tmp/%s baytları yerleştirilen değil (sha256 %s)" % (GAS_DOSYASI, hashlib.sha256(veri).hexdigest()[:12]))
    return sorun


def tur_adi(yol):
    """lstat türü (bağ izlenmez): dizin / dosya / bağ / diğer / okunamadı. Ad ASCII kaçışlı yazılır (günlük satırı enjeksiyonu olmasın)."""
    try:
        kip = os.lstat(yol).st_mode
    except OSError:
        return "okunamadı"
    if stat.S_ISLNK(kip):
        return "bağ"
    if stat.S_ISDIR(kip):
        return TUR_DIZIN
    if stat.S_ISREG(kip):
        return TUR_DOSYA
    return "diğer"


def tmp_ust_sorunlari(work, asama):
    """work/.tmp gerçek dizin mi ve üst düzey girdileri TMP_UST_ONCE / TMP_UST_SONRA kümesine TAM eşit mi (fazla da eksik de kırmızı)? Türler lstat ile;
    gas-preprocessor.pl'in kendisini gas_dosyasi_sorunlari denetler."""
    tmp = os.path.join(work, ".tmp")
    tur = tur_adi(tmp)
    if tur == "okunamadı":
        return [".tmp yok"]
    if tur != TUR_DIZIN:
        return [".tmp gerçek dizin değil (%s)" % tur]
    beklenen = TMP_UST_ONCE if asama == "once" else TMP_UST_SONRA
    try:
        mevcut = os.listdir(tmp)
    except OSError:
        return [".tmp listelenemedi"]
    sorun = []
    for ad in sorted(set(mevcut) - set(beklenen)):
        sorun.append(".tmp/%s kilitte yok (%s)" % (ascii(ad)[1:-1], tur_adi(os.path.join(tmp, ad))))
    for ad in sorted(beklenen):
        if ad == GAS_DOSYASI:
            continue
        if ad not in mevcut:
            sorun.append(".tmp/%s eksik (%s olmalı)" % (ad, beklenen[ad]))
        elif tur_adi(os.path.join(tmp, ad)) != beklenen[ad]:
            sorun.append(".tmp/%s gerçek %s değil (%s)" % (ad, beklenen[ad], tur_adi(os.path.join(tmp, ad))))
    return sorun


def ic_gitlink_uygun(ad, yol_b, sha, ic_kilit):
    """İ1 (SK-936-1): gitlink, tablodaki iç içe kaynağın üst ağacında, tam yolunda ve kilitteki tam commit'iyle mi?
    Yol bayt bayt karşılaştırılır (büyük-küçük harf, sondaki / ya da ek bileşen eşleşmez). Eşleşen iç içe kaynağın adı ya da None döner."""
    for n, (ust, goreli) in IC_KAYNAKLAR.items():
        if ust == ad and yol_b == goreli.encode("utf-8") and ic_kilit.get(n) == sha:
            return n
    return None


def altmodul_sorunlari(yol, commit, ad, ic_kilit):
    """Kilitli commit'in ağacında gitlink (160000) ve .gitmodules için sorun listesi döner (SK-893-9, SK-936-1).
    Yalnız İ1 (iç içe kaynağın gitlink'i) ve İ2 (kökte izinli .gitmodules blob'u) serbesttir; tablo build.py'dedir.
    ic_kilit: kilitteki iç içe kaynakların {ad: commit} haritası."""
    sorun = []
    out = git_ok(["ls-tree", "-r", "-z", "--full-tree", commit], yol)
    gitlinkler, gitmodules = [], []
    for kayit in out.split(b"\0"):
        if not kayit:
            continue
        bilgi, _, yol_b = kayit.partition(b"\t")
        mod, _, sha = bilgi.split(b" ")
        if mod == b"160000":
            gitlinkler.append((yol_b, sha.decode("ascii")))
        if any(b.lower() == b".gitmodules" for b in yol_b.split(b"/")):
            gitmodules.append((yol_b, mod, sha))
    uygun = [g for g in gitlinkler if ic_gitlink_uygun(ad, g[0], g[1], ic_kilit) is not None]
    for g in gitlinkler:
        if g not in uygun:
            sorun.append("gitlink (alt modül) var: %s" % g[0].decode("utf-8", "replace")[:80])
    if len(gitlinkler) > 1:
        sorun.append("birden fazla gitlink var (en çok bir İ1 gitlink'i serbest)")
    for n, c in ic_kilit.items():
        if IC_KAYNAKLAR[n][0] == ad and not any(ic_gitlink_uygun(ad, g[0], g[1], ic_kilit) == n for g in gitlinkler):
            sorun.append("kilitte %s var ama ağaçta İ1 gitlink'i yok (%s)" % (n, IC_KAYNAKLAR[n][1]))
    i1 = len(gitlinkler) == 1 and len(uygun) == 1
    for yol_b, mod, sha in gitmodules:
        if yol_b != b".gitmodules":
            sorun.append("kökte olmayan ya da farklı harfli .gitmodules var: %s" % yol_b.decode("utf-8", "replace")[:80])
        elif mod != b"100644":
            sorun.append(".gitmodules normal dosya değil")
        elif sha == GITMODULES_BOS:
            if gitlinkler:
                sorun.append(".gitmodules boş ama ağaçta gitlink var")
        elif sha == GITMODULES_FREETYPE:
            if not (i1 and ad in [ust for ust, _ in IC_KAYNAKLAR.values()]):
                sorun.append(".gitmodules FreeType blob'u ama İ1 sağlanmıyor")
        else:
            sorun.append(".gitmodules var (izinli blob değil)")
    return sorun


# Next kök ağacının içinde durup ayrı kilitli kaynak olan (kendi denetlenen) ya da derleme çıktısı olan üst dizinler.
NEXT_ICI_DIZINLER = frozenset(["src", ".tmp", "prebuilt"])


def agac_ici_sorunlari(yol, commit, ad):
    """Kilitli ağacın İÇİNE derleme sırasında inen kaynakları arar (SK-893-9'un kapsamı üst düzey adlarla bitmez).
    Örnek yollar: FreeType autogen.sh `git submodule update --init` (iç .git, .git/modules), HarfBuzz meson wrap'leri
    (subprojects/<x>/). (1) kök .git dışında .git girdisi ve .git/modules yok; (2) commit ağacında dizin olarak
    bulunmayan hiçbir subprojects/<x>/ yok (ağaçta gitlink olarak duran yol dizin sayılmaz).
    Tek istisna İ1: tablodaki iç içe kaynağın tam yolu (örn. freetype subprojects/dlg). Orada .git dizini olması beklenir ve içeriği
    o kaynağın kendi denetimine bırakılır (HEAD kilide eşit, durum boş); yol eşitliği tamdır, önek eşleşmesi yoktur (SK-936-5)."""
    sorun = []
    ic_yollar = set(goreli for ust, goreli in IC_KAYNAKLAR.values() if ust == ad)
    if os.path.lexists(os.path.join(yol, ".git", "modules")):
        sorun.append(".git/modules var (alt modül indirilmiş)")
    out = git_ok(["ls-tree", "-r", "-t", "-z", "--full-tree", commit], yol)
    dizinler = set()
    for kayit in out.split(b"\0"):
        if not kayit:
            continue
        bilgi, _, yol_b = kayit.partition(b"\t")
        if bilgi.split(b" ")[1] == b"tree":
            dizinler.add(yol_b.decode("utf-8", "surrogateescape"))
    for kok, alt, dosyalar in os.walk(yol):
        goreli = os.path.relpath(kok, yol).replace(os.sep, "/")
        if goreli == ".":
            goreli = ""
            alt[:] = [d for d in alt if d != ".git" and not (ad == NEXT_ADI and d in NEXT_ICI_DIZINLER)]
        else:
            for g in alt + dosyalar:
                if g.lower() == ".git":
                    sorun.append("%s/.git var (iç içe depo ya da indirilmiş alt modül)" % goreli)
            alt[:] = [d for d in alt if d != ".git"]
        if goreli.split("/")[-1] == "subprojects":
            for d in list(alt):
                tam = (goreli + "/" + d)
                if tam in ic_yollar and not os.path.islink(os.path.join(kok, d)):
                    alt.remove(d)
                    continue
                if tam not in dizinler:
                    sorun.append("%s/ kilitli commit'in ağacında dizin değil (indirilmiş olabilir)" % tam)
                    alt.remove(d)
    return sorun


def kilit_dogrula(work, kilit, asama, dosya_izni=False):
    """İki yönlü kilit denetimi. asama "once": (a)(b)(c)(d); "sonra": (a)(b)(d) (Next ve yamalarımız ağaçları kirletir).
    Her iki aşamada ayrıca gitlink/.gitmodules istisna tablosu (altmodul_sorunlari) ve ağaç içi indirme denetimi (agac_ici_sorunlari) koşar.
    İç içe kaynak (dlg) için "sonra" kipinde de HEAD kilide eşit ve durum boş olmalıdır.
    İki kipte de (e) .tmp/gas-preprocessor.pl baytları ve (f) .tmp/ üst düzey kümesi (kipe göre farklı tam küme) denetlenir."""
    kirmizi_if(asama not in ("once", "sonra"), "asama once ya da sonra olmalı")
    sorunlar = []
    ic_kilit = dict((k["name"], k["commit"]) for k in kilit["sources"] if k["name"] in IC_KAYNAKLAR)
    for k in kilit["sources"]:
        ad, yol = k["name"], agac_yolu(work, k["name"])
        if not os.path.isdir(os.path.join(yol, ".git")):
            sorunlar.append("%s: git deposu değil ya da yok" % ad)
            continue
        rc, out, _ = git(["rev-parse", "HEAD"], yol)
        if rc != 0 or out.decode().strip() != k["commit"]:
            sorunlar.append("%s: HEAD kilitteki commit değil" % ad)
        # İç içe kaynağa (dlg) derleme hiçbir şey yazmamalı; bu yüzden "sonra" kipinde de durumu boş olmalı (SK-936-5).
        if asama == "once" or ad in IC_KAYNAKLAR:
            # Next kök ağacının içinde src/ ve .tmp/ (diğer kaynaklar) durur; bunlar Next için "kirli" sayılmaz.
            durum = ["status", "--porcelain"] + (["--", ".", ":(exclude)src", ":(exclude).tmp"] if ad == NEXT_ADI else [])
            rc, out, _ = git(durum, yol)
            if rc != 0 or out.strip():
                sorunlar.append("%s: ağaç temiz değil" % ad)
        try:
            for s in altmodul_sorunlari(yol, k["commit"], ad, ic_kilit) + agac_ici_sorunlari(yol, k["commit"], ad):
                sorunlar.append("%s: %s" % (ad, s))
        except Kirmizi as e:
            sorunlar.append("%s: %s" % (ad, e))
    # (b) src/ ve .tmp/source/ altında kilitte olmayan hiçbir girdi yok; Next'in kendi indirmesi de buraya düşer.
    for ust, beklenen in ((os.path.join(work, "src"), [a for a in KAYNAK_ADLARI if a != NEXT_ADI and a not in TMP_KAYNAKLARI and a not in IC_KAYNAKLAR]),
                          (os.path.join(work, ".tmp", "source"), list(TMP_KAYNAKLARI))):
        if not os.path.isdir(ust):
            sorunlar.append("%s yok" % os.path.relpath(ust, work).replace(os.sep, "/"))
            continue
        for girdi in sorted(os.listdir(ust)):
            if girdi not in beklenen:
                sorunlar.append("%s/%s kilitte yok" % (os.path.relpath(ust, work).replace(os.sep, "/"), girdi))
    # (e) gas-preprocessor.pl iki kipte de yerleştirildiği baytlarla aynı olmalı (Next onu AS olarak çalıştırır).
    sorunlar += gas_dosyasi_sorunlari(work)
    # (f) .tmp/ üst düzeyi iki kipte de kapalı kümeye tam eşit ve türleri lstat ile doğru (TMP_UST_ONCE / TMP_UST_SONRA, SK-951-1…3).
    sorunlar += tmp_ust_sorunlari(work, asama)
    if sorunlar:
        raise Kirmizi("kilit denetimi (%s) başarısız:\n  - %s" % (asama, "\n  - ".join(sorunlar[:30])))
    print("verify-lock (%s): %d kaynak kilitle aynı, kilitte olmayan dizin yok" % (asama, len(kilit["sources"])))


# MARK: - Kaynak arşivi

class BlobOkuyucu(object):
    """git cat-file --batch: blob baytlarını nesne veritabanından olduğu gibi okur (öznitelik ya da export-ignore etkisi yok)."""

    def __init__(self, yol):
        self.p = subprocess.Popen(["git"] + GIT_SABIT + ["cat-file", "--batch"], cwd=yol, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def oku(self, sha):
        self.p.stdin.write(sha + b"\n")
        self.p.stdin.flush()
        baslik = self.p.stdout.readline().split()
        kirmizi_if(len(baslik) != 3 or baslik[1] != b"blob", "blob okunamadı")
        boyut = int(baslik[2])
        veri = self.p.stdout.read(boyut)
        kirmizi_if(len(veri) != boyut or self.p.stdout.read(1) != b"\n", "blob eksik okundu")
        return veri

    def kapat(self):
        try:
            self.p.stdin.close()
        finally:
            self.p.wait()


def etiket_gecerli(etiket):
    return ETIKET_RE.fullmatch(etiket) is not None or DENEME_RE.fullmatch(etiket) is not None


def arsiv_oneki(ad):
    """Kaynağın arşivdeki dizin öneki: iç içe kaynak üst ağacının içinde, gitlink'in yerinde durur (SK-936-4)."""
    if ad in IC_KAYNAKLAR:
        ust, goreli = IC_KAYNAKLAR[ad]
        return arsiv_oneki(ust) + "/" + goreli
    return ad


def arsiv_yaz(work, kilit, etiket, cikti_dizini, v15_dosyalar):
    """Kilidi geçmiş ağaçlardan (commit nesnelerinden, çalışma ağacından değil) tek bir kaynak arşivi üretir.
    İ1'e uyan gitlink dosya olarak yazılmaz; yerine iç içe kaynağın ağacı yazılır, böylece açılan FreeType ağacı tamdır."""
    kirmizi_if(not etiket_gecerli(etiket), "etiket biçimi geçersiz")
    os.makedirs(cikti_dizini, exist_ok=True)
    hedef = os.path.join(cikti_dizini, "forge-ffmpeg-src-%s.tar.xz" % etiket)
    kirmizi_if(os.path.lexists(hedef), "arşiv zaten var")
    gecici = hedef + ".tmp"
    kayitlar = []
    yazilan = set()
    ic_kilit = dict((k["name"], k["commit"]) for k in kilit["sources"] if k["name"] in IC_KAYNAKLAR)

    def uye_ekle(tar, ti, dosya=None):
        kirmizi_if(ti.name in yazilan, "arşivde yinelenen ad: %s" % ti.name)
        yazilan.add(ti.name)
        tar.addfile(ti, dosya)

    with tarfile.open(gecici, "w:xz", preset=6) as tar:
        for k in kilit["sources"]:
            ad, yol = k["name"], agac_yolu(work, k["name"])
            agac = git_metin(["rev-parse", "%s^{tree}" % k["commit"]], yol)
            tarih = int(git_metin(["show", "-s", "--format=%ct", k["commit"]], yol))
            kaynak_kaydi = {"name": ad, "url": k["url"], "ref": k["ref"], "commit": k["commit"], "tree": agac}
            if ad in IC_KAYNAKLAR:
                kaynak_kaydi["parent"], kaynak_kaydi["path"] = IC_KAYNAKLAR[ad]
            kayitlar.append(kaynak_kaydi)
            okuyucu = BlobOkuyucu(yol)
            try:
                out = git_ok(["ls-tree", "-r", "-z", "--full-tree", k["commit"]], yol)
                for kayit in out.split(b"\0"):
                    if not kayit:
                        continue
                    bilgi, _, yol_b = kayit.partition(b"\t")
                    mod, tip, sha = bilgi.split(b" ")
                    try:
                        goreli = yol_b.decode("utf-8")
                    except UnicodeDecodeError:
                        raise Kirmizi("%s: UTF-8 olmayan yol" % ad)
                    kirmizi_if(goreli.startswith("/") or ".." in goreli.split("/"), "%s: güvensiz yol" % ad)
                    if mod == b"160000" and ic_gitlink_uygun(ad, yol_b, sha.decode("ascii"), ic_kilit) is not None:
                        continue
                    kirmizi_if(tip != b"blob", "%s: blob olmayan girdi (alt modül?)" % ad)
                    veri = okuyucu.oku(sha)
                    ti = tarfile.TarInfo("%s/%s" % (arsiv_oneki(ad), goreli))
                    ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = tarih, 0, 0, "", ""
                    if mod == b"120000":
                        ti.type, ti.linkname, ti.size, ti.mode = tarfile.SYMTYPE, veri.decode("utf-8"), 0, 0o777
                        uye_ekle(tar, ti)
                    elif mod in (b"100644", b"100755"):
                        ti.size, ti.mode = len(veri), (0o755 if mod == b"100755" else 0o644)
                        uye_ekle(tar, ti, io.BytesIO(veri))
                    else:
                        raise Kirmizi("%s: bilinmeyen dosya kipi" % ad)
            finally:
                okuyucu.kapat()
        for ad_, veri in (("SOURCES.json", (json.dumps(kayitlar, indent=2, sort_keys=True) + "\n").encode("utf-8")),):
            ti = tarfile.TarInfo(ad_)
            ti.size, ti.mode, ti.mtime = len(veri), 0o644, 0
            uye_ekle(tar, ti, io.BytesIO(veri))
        for arsiv_adi, yerel in v15_dosyalar:
            with open(yerel, "rb") as f:
                veri = f.read()
            ti = tarfile.TarInfo("forge-ffmpeg/" + arsiv_adi)
            ti.size, ti.mode, ti.mtime = len(veri), 0o644, 0
            uye_ekle(tar, ti, io.BytesIO(veri))
    os.replace(gecici, hedef)
    print("archive: %s (%d bayt)" % (os.path.basename(hedef), os.path.getsize(hedef)))
    return hedef


def v15_dosya_listesi():
    """Arşive giren V15 dosyaları: betik, kilit, yamalar, iş akışı."""
    sonuc = [("build.py", os.path.join(HERE, "build.py")), ("lock.json", os.path.join(HERE, "lock.json"))]
    pd = os.path.join(HERE, "patches")
    if os.path.isdir(pd):
        for ad in sorted(os.listdir(pd)):
            sonuc.append(("patches/" + ad, os.path.join(pd, ad)))
    wf = os.path.join(HERE, ".github", "workflows", "build.yml")
    if os.path.isfile(wf):
        sonuc.append((".github/workflows/build.yml", wf))
    return sonuc


# MARK: - Yamalar

YAMA_ANAHTARLARI = set(["id", "tree", "file", "op", "anchor", "text", "project", "license", "date", "notice", "reason"])


def yamalari_oku(yama_dizini):
    yamalar = []
    kirmizi_if(not os.path.isdir(yama_dizini), "patches/ dizini yok")
    for ad in sorted(os.listdir(yama_dizini)):
        if not ad.endswith(".json"):
            raise Kirmizi("patches/%s: yalnız .json yama tanımı olabilir" % ad)
        try:
            with open(os.path.join(yama_dizini, ad), "rb") as f:
                y = json.loads(f.read().decode("utf-8"), object_pairs_hook=_yinelenen_anahtar_yok)
        except (OSError, UnicodeDecodeError, ValueError):
            raise Kirmizi("patches/%s okunamadı" % ad)
        kirmizi_if(not isinstance(y, dict) or set(y) != YAMA_ANAHTARLARI, "patches/%s: anahtarlar beklenen küme değil" % ad)
        for a in YAMA_ANAHTARLARI:
            kirmizi_if(not isinstance(y[a], str) or not y[a], "patches/%s: %s boş ya da dize değil" % (ad, a))
        kirmizi_if(TARIH_RE.fullmatch(y["date"]) is None, "patches/%s: date YYYY-MM-DD değil" % ad)
        kirmizi_if(y["op"] not in ("insert_before", "replace"), "patches/%s: op bilinmiyor" % ad)
        kirmizi_if(y["tree"] not in KAYNAK_ADLARI, "patches/%s: tree kilitte yok" % ad)
        yamalar.append(y)
    kirmizi_if(not yamalar, "hiç yama tanımı yok")
    return yamalar


def yama_uygula(work, yama_dizini):
    """Her yama çapasını tam bir kez bulmalı (0 ve ≥2 kırmızı) ve dosya baytları değişmeli (SK-893-9, bağlayıcı 6)."""
    rapor = []
    for y in yamalari_oku(yama_dizini):
        kok = os.path.realpath(agac_yolu(work, y["tree"]))
        hedef = os.path.realpath(os.path.join(kok, y["file"]))
        kirmizi_if(os.path.commonpath([kok, hedef]) != kok or hedef == kok, "%s: dosya ağacın dışında" % y["id"])
        kirmizi_if(not os.path.isfile(hedef), "%s: dosya yok: %s" % (y["id"], y["file"]))
        with open(hedef, "rb") as f:
            eski = f.read()
        try:
            metin = eski.decode("utf-8")
        except UnicodeDecodeError:
            raise Kirmizi("%s: dosya UTF-8 değil" % y["id"])
        sayi = metin.count(y["anchor"])
        kirmizi_if(sayi != 1, "%s: çapa %d kez bulundu (tam bir kez olmalı); yama uygulanmadı" % (y["id"], sayi))
        if y["op"] == "insert_before":
            yeni = metin.replace(y["anchor"], y["text"] + y["anchor"], 1)
        else:
            yeni = metin.replace(y["anchor"], y["text"], 1)
        kirmizi_if(yeni == metin, "%s: dosya değişmedi" % y["id"])
        with open(hedef, "wb") as f:
            f.write(yeni.encode("utf-8"))
        rapor.append("%s %s/%s %s -> %s" % (y["id"], y["tree"], y["file"],
                                           hashlib.sha256(eski).hexdigest()[:12], hashlib.sha256(yeni.encode("utf-8")).hexdigest()[:12]))
        print("patch: %s uygulandı (%s)" % (y["id"], y["file"]))
    return rapor


LIBPNG_RE = re.compile(rb"\bTARGET_OS_MAC\b")


def libpng_olcumu(work):
    """libpng fp.h yamasının gerekmediğini kilitli ağaçtan ölçer: pngpriv.h'de TARGET_OS_MAC koşulu varsa kırmızı.
    Kilitte libpng değişince koşul gelebilir; sessiz geçilmez. Dönen satır toolchain.txt'nin yama bölümüne girer."""
    yol = os.path.join(agac_yolu(work, "libpng"), "pngpriv.h")
    kirmizi_if(not os.path.isfile(yol), "libpng: pngpriv.h yok; fp.h yamasının gerekip gerekmediği ölçülemedi")
    with open(yol, "rb") as f:
        veri = f.read()
    kirmizi_if(LIBPNG_RE.search(veri) is not None,
               "libpng: pngpriv.h TARGET_OS_MAC içeriyor; fp.h yaması gerekebilir, yama tanımı eklenmeden derlenmez")
    return "libpng pngpriv.h: TARGET_OS_MAC yok, fp.h yaması uygulanmadı (sha256 %s)" % hashlib.sha256(veri).hexdigest()[:12]


# MARK: - Araçlar ve derleme

def calistir_satir(komut, env=None):
    """Komutun ilk çıktı satırı; çalışmazsa 'okunamadı'."""
    try:
        p = subprocess.run(komut, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return "okunamadı"
    satirlar = p.stdout.decode("utf-8", "replace").strip().splitlines()
    return satirlar[0].strip() if satirlar else "okunamadı"


def temiz_env():
    """Derleme alt sürecinin ortamı: miras ortam aktarılmaz; belirteç, kimlik bilgisi ve CI değişkenleri girmez."""
    return {"PATH": TEMEL_PATH, "HOME": os.path.expanduser("~"), "TMPDIR": "/tmp", "LANG": "en_US.UTF-8",
            "GIT_TERMINAL_PROMPT": "0", "DEVELOPER_DIR": XCODE_UYGULAMA + "/Contents/Developer"}


def brew_prefix(formul, calistir=subprocess.run):
    env = temiz_env()
    env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
    try:
        p = calistir(["brew", "--prefix", formul], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        raise Kirmizi("brew --prefix %s çalıştırılamadı" % formul)
    yol = p.stdout.decode("utf-8", "replace").strip()
    kirmizi_if(p.returncode != 0 or not os.path.isdir(yol), "Homebrew formülü kurulu değil: %s" % formul)
    return yol


def derleme_env():
    env = temiz_env()
    ekler = [os.path.join(brew_prefix(f), "bin") for f in BREW_YOL_FORMULLERI]
    env["PATH"] = ":".join(ekler + [env["PATH"]])
    gettext = brew_prefix("gettext")
    env["ACLOCAL_PATH"] = "%s/share/aclocal:/opt/homebrew/share/aclocal:/usr/local/share/aclocal" % gettext
    gsed = os.path.join(brew_prefix("gnu-sed"), "bin", "gsed")
    kirmizi_if(not os.access(gsed, os.X_OK), "GNU sed bulunamadı (gnu-sed kurulu olmalı)")
    env["SED"] = gsed
    sorun = env_sorunlari(env)
    kirmizi_if(sorun, "derleme ortamı izin listesine uymuyor:\n  - " + "\n  - ".join(sorun))
    return env


def env_sorunlari(env):
    """Derleme ortamı yalnız izin listesindeki değişkenleri taşır; PKG_CONFIG_PATH ayrıca adıyla yasaktır."""
    sorun = []
    if "PKG_CONFIG_PATH" in env:
        sorun.append("PKG_CONFIG_PATH tanımlı olmamalı (Homebrew .pc dosyaları derlemeye sızar)")
    for a in sorted(env):
        if a not in ENV_IZINLI:
            sorun.append("izinli olmayan değişken: %s" % a)
    return sorun


def araclari_denetle():
    """Xcode tam yolla ve build numarasıyla sabittir (bağlayıcı 2); gerekli araçlar bulunmalı."""
    kirmizi_if(not os.path.isdir(XCODE_UYGULAMA), "Xcode bulunamadı: %s" % XCODE_UYGULAMA)
    env = temiz_env()
    out = subprocess.run(["xcodebuild", "-version"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
    satirlar = out.stdout.decode("utf-8", "replace").splitlines()
    kirmizi_if(out.returncode != 0 or ("Build version " + XCODE_BUILD) not in satirlar,
               "xcodebuild build numarası %s değil (imaj Xcode'u değişmişse yeni V15 commit'i ve yeni etiket gerekir)" % XCODE_BUILD)
    path = derleme_env()["PATH"].split(":")
    eksik = [a for a in GEREKLI_ARACLAR if not any(os.access(os.path.join(p, a), os.X_OK) for p in path)]
    kirmizi_if(eksik, "gerekli araç bulunamadı: %s" % ", ".join(eksik))
    print("tools: Xcode %s (%s) ve %d araç tamam" % (XCODE_UYGULAMA, XCODE_BUILD, len(GEREKLI_ARACLAR)))


def _grubu_oldur(p):
    """Süreç grubuna önce SIGTERM, kısa beklemeden sonra koşulsuz SIGKILL (SIGTERM'i yutan torunlar için)."""
    for sinyal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(p.pid, sinyal)
        except OSError:
            pass
        if sinyal == signal.SIGTERM:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                pass


def zamanli_calistir(komut, cwd, env, sessizlik_siniri, kontrol_araligi=1.0, cikti=None, saat=time.monotonic):
    """Alt süreci kendi süreç grubunda koşar ve stdout'unu aynen aktarır. `sessizlik_siniri` saniye boyunca tek bayt bile
    gelmezse grubun tamamı öldürülür. (çıkış kodu, süre aşıldı mı, son çıktının son baytları) döner. Yalnız POSIX.
    Next kütüphaneleri `ad: ` yazıp `ok` ile bitirir ve ayrıntıyı build.log'a yazar: bağımlılık bekleme döngüsü gibi bir takılmada
    stdout susar, bu yüzden sessizlik bir kütüphanenin (ya da kütüphaneler arası zamanlayıcının) ilerlemediğini gösterir."""
    cikti = sys.stdout.buffer if cikti is None else cikti
    p = subprocess.Popen(komut, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, start_new_session=True)
    durum = {"son": saat(), "kuyruk": b""}

    def oku():
        while True:
            parca = os.read(p.stdout.fileno(), 65536)
            if not parca:
                return
            durum["son"] = saat()
            durum["kuyruk"] = (durum["kuyruk"] + parca)[-400:]
            try:
                cikti.write(parca)
                cikti.flush()
            except (OSError, ValueError):
                pass

    okuyucu = threading.Thread(target=oku, daemon=True)
    okuyucu.start()
    asildi = False
    while p.poll() is None:
        if saat() - durum["son"] > sessizlik_siniri:
            asildi = True
            _grubu_oldur(p)
            break
        time.sleep(kontrol_araligi)
    p.wait()
    okuyucu.join(5)
    return p.returncode, asildi, durum["kuyruk"]


def log_sonu(yol, bayt=LOG_SONU_BAYT):
    """Dosyanın son `bayt` baytı (yarım ilk satır atılır). build.log bekleme döngüsünde gigabaytlara çıkabilir: bütünü okunmaz."""
    boyut = os.path.getsize(yol)
    with open(yol, "rb") as f:
        f.seek(max(0, boyut - bayt))
        veri = f.read(bayt)
    metin = veri.decode("utf-8", "replace")
    if boyut > bayt and "\n" in metin:
        metin = metin.split("\n", 1)[1]
    return metin


RE_BEKLEYEN = re.compile(r"INFO: Skipping ([A-Za-z0-9_.+-]+), dependencies built=0")

# Next'in download() işlevi (scripts/function.sh) sonucunu build.log'a yalnız bu iki satırla bildirir. İndirme bu yapıda hiç beklenmez
# (gas-preprocessor.pl yerleştirilir, geri kalanı kilitli kaynaktır); başarılı bir indirme de, başarısız olan da kırmızıdır.
# clone_git_repository_with_* kendi satırlarını yazar, onların hedefleri verify-lock (b) kapsamındadır.
INDIRME_IZLERI = (b"DEBUG: Downloaded ", b"ERROR: Failed to download")


def indirme_izleri(yol, izler=INDIRME_IZLERI, blok=1 << 20):
    """build.log'ta Next indirme satırlarının sayısı: {iz: (sayı, ilk bayt konumu)}. Dosya gigabayt olabilir: sabit boyutlu bloklarla,
    blok sınırını aşan eşleşmeler için son len(iz)-1 baytı bir sonraki bloğa taşıyarak taranır; bütünü belleğe alınmaz."""
    sonuc = {}
    tasi = max(len(i) for i in izler) - 1
    kuyruk = b""
    taban = 0       # kuyruk[0]'ın dosyadaki konumu
    with open(yol, "rb") as f:
        while True:
            parca = f.read(blok)
            if not parca:
                break
            veri = kuyruk + parca
            for iz in izler:
                bas = 0
                while True:
                    i = veri.find(iz, bas)
                    if i < 0:
                        break
                    bas = i + 1
                    if i + len(iz) <= len(kuyruk):
                        continue        # önceki blokta zaten sayıldı
                    n, ilk = sonuc.get(iz, (0, taban + i))
                    sonuc[iz] = (n + 1, ilk)
            kuyruk = veri[-tasi:] if tasi else b""
            taban += len(veri) - len(kuyruk)
    return sonuc


def indirme_mesaji(sonuc):
    return "build.log'da Next indirme satırı var: " + ", ".join("'%s' ×%d (ilk bayt %d)" % (iz.decode("ascii").strip(), n, ilk) for iz, (n, ilk) in sorted(sonuc.items()))


def bekleyen_kitapliklar(metin):
    """build.log sonunda 'bağımlılığı hazır değil' diye atlanan kütüphaneler (sıralı, yinelenmez)."""
    return sorted(set(RE_BEKLEYEN.findall(metin)))


def derle(work, env=None, komut=None, sessizlik_siniri=SESSIZLIK_SINIRI, calistir=zamanli_calistir):
    """Next ios.sh'ı temiz ortamla koşar; çıktı dizinini döndürür. Next build.log'u release'e girmez.
    ios.sh `sessizlik_siniri` saniye hiç çıktı vermezse süreç grubu öldürülür ve kırmızı olur (tarihçe: ilk deneme koşusu 4 saat takıldı)."""
    env = derleme_env() if env is None else env
    komut = ["/bin/bash", "ios.sh"] + IOS_SECENEKLERI if komut is None else komut
    print("build: %s" % " ".join(komut))
    sys.stdout.flush()
    rc, asildi, son = calistir(komut, work, env, sessizlik_siniri)
    log = os.path.join(work, "build.log")
    indirme = indirme_izleri(log) if os.path.isfile(log) else {}
    if rc != 0 or asildi:
        bekleyen = []
        if os.path.isfile(log):
            sonu = log_sonu(log)
            bekleyen = bekleyen_kitapliklar(sonu)
            print("build.log: %d bayt; son 150 satır:" % os.path.getsize(log), file=sys.stderr)
            print(gizle("\n".join(sonu.splitlines()[-150:])), file=sys.stderr)
        ek = "; " + indirme_mesaji(indirme) if indirme else ""
        if asildi:
            raise Kirmizi("ios.sh %d sn hiç çıktı vermedi, süreç grubu öldürüldü; son çıktı: %r; build.log sonunda bağımlılığı bekleyen kütüphaneler: %s%s"
                          % (sessizlik_siniri, gizle(son.decode("utf-8", "replace"))[-80:], ", ".join(bekleyen) or "yok", ek))
        raise Kirmizi("ios.sh başarısız (kod %d)%s" % (rc, ek))
    kirmizi_if(indirme, indirme_mesaji(indirme))
    return cikti_dizini_bul(work)


def cikti_dizini_bul(work):
    pb = os.path.join(work, "prebuilt")
    adaylar = [d for d in sorted(os.listdir(pb)) if d.startswith("bundle-apple-xcframework") and os.path.isdir(os.path.join(pb, d))] if os.path.isdir(pb) else []
    kirmizi_if(len(adaylar) != 1, "prebuilt/ altında tam bir bundle-apple-xcframework* dizini olmalı (%d bulundu)" % len(adaylar))
    return os.path.join(pb, adaylar[0])


# MARK: - Tanı

# Başarısız ya da iptal olan koşunun günlükleri (iş akışındaki tani adımı). Next tek bir work/build.log yazar (kütüphane başına günlük
# yoktur); kütüphane başına olan tek şey autoconf/meson/cmake'in kendi günlükleridir. Hepsi boyut sınırlıdır: bekleme döngüsünde
# build.log gigabaytlara çıkabilir ve artifact'a bütünüyle girmez.
TANI_LOG_BAS = 512 * 1024
TANI_LOG_SON = 2 * 1024 * 1024
TANI_DOSYA_SON = 256 * 1024
TANI_TOPLAM = 24 * 1024 * 1024
TANI_DOSYA_ADLARI = frozenset(["config.log", "meson-log.txt", "CMakeError.log", "CMakeOutput.log"])
# FFmpeg kaynak ağacından (work/src/ffmpeg) tam göreli adlarla toplanan 11 dosya (SK-943-7): derlenmiş küme listelerinin gerçek baytları,
# configure'ın yazdığı yapılandırma dizesi (config.h, config.fate). Glob yok; başka ad eklemek yeni bir güvenlik turu ister.
TANI_FFMPEG_DOSYALARI = (
    "libavformat/protocol_list.c", "libavformat/demuxer_list.c", "libavformat/muxer_list.c",
    "libavcodec/codec_list.c", "libavcodec/parser_list.c", "libavcodec/bsf_list.c",
    "libavfilter/filter_list.c", "libavdevice/indev_list.c", "libavdevice/outdev_list.c",
    "ffbuild/config.fate", "config.h",
)


def _bas_son(yol, bas, son):
    """Küçükse bütünü; değilse ilk `bas` ve son `son` bayt, arada kaç baytın atlandığı yazılı."""
    boyut = os.path.getsize(yol)
    with open(yol, "rb") as f:
        if boyut <= bas + son:
            return f.read(), boyut
        ilk = f.read(bas)
        f.seek(boyut - son)
        return ilk + ("\n[... %d bayt atlandı ...]\n" % (boyut - bas - son)).encode("utf-8") + f.read(son), boyut


def _tani_yaz(hedef, veri):
    with open(hedef, "wb") as f:
        f.write(gizle(veri.decode("utf-8", "replace")).encode("utf-8"))


def tani_topla(work, out):
    """work'teki build.log (baş+son), kütüphane günlükleri, FFmpeg derlenmiş küme listeleri ve yapılandırma dosyaları (TANI_FFMPEG_DOSYALARI) ve prebuilt ile .tmp/ üst düzey listeleri
    out'a (yeni dizin) gizlenerek ve sınırlı kopyalanır."""
    if not os.path.isdir(work):
        print("tani: çalışma dizini yok, toplanacak bir şey yok")
        return 0
    kirmizi_if(os.path.lexists(out), "tanı çıktı dizini zaten var")
    os.makedirs(out)
    indeks, toplam = [], 0
    log = os.path.join(work, "build.log")
    if os.path.isfile(log) and not os.path.islink(log):
        veri, boyut = _bas_son(log, TANI_LOG_BAS, TANI_LOG_SON)
        _tani_yaz(os.path.join(out, "build.log"), veri)
        toplam += len(veri)
        indeks.append("build.log: kaynak %d bayt, yazılan %d bayt" % (boyut, len(veri)))
        bekleyen = bekleyen_kitapliklar(log_sonu(log))
        indeks.append("build.log sonunda bağımlılığı bekleyen kütüphaneler: %s" % (", ".join(bekleyen) or "yok"))
    else:
        indeks.append("build.log yok")
    kaynak = os.path.join(work, "src")
    if os.path.isdir(kaynak):
        for kok, dizinler, dosyalar in os.walk(kaynak, followlinks=False):
            dizinler.sort()
            for ad in sorted(dosyalar):
                yol = os.path.join(kok, ad)
                if ad not in TANI_DOSYA_ADLARI or os.path.islink(yol) or not os.path.isfile(yol):
                    continue
                goreli = os.path.relpath(yol, work).replace(os.sep, "__")
                if toplam >= TANI_TOPLAM:
                    indeks.append("%s: toplam sınırı aşıldı, alınmadı" % goreli)
                    continue
                veri, boyut = _bas_son(yol, 0, TANI_DOSYA_SON)
                _tani_yaz(os.path.join(out, goreli), veri)
                toplam += len(veri)
                indeks.append("%s: kaynak %d bayt, yazılan %d bayt" % (goreli, boyut, len(veri)))
    ffmpeg_agaci = agac_yolu(work, "ffmpeg")
    for goreli in TANI_FFMPEG_DOSYALARI:
        yol = os.path.join(ffmpeg_agaci, *goreli.split("/"))
        ad = os.path.relpath(yol, work).replace(os.sep, "__")
        # Yolun hiçbir bileşeni (work altında) sembolik bağ olamaz: bağ izlenip dışarıdaki bir dosya artifact'a girmesin.
        bilesenler = os.path.relpath(yol, work).split(os.sep)
        bag = any(os.path.islink(os.path.join(work, *bilesenler[:n])) for n in range(1, len(bilesenler) + 1))
        if bag or not os.path.isfile(yol):
            indeks.append("%s: yok ya da sembolik bağ, alınmadı" % ad)
            continue
        if toplam >= TANI_TOPLAM:
            indeks.append("%s: toplam sınırı aşıldı, alınmadı" % ad)
            continue
        veri, boyut = _bas_son(yol, TANI_DOSYA_SON, 0)
        _tani_yaz(os.path.join(out, ad), veri)
        toplam += len(veri)
        indeks.append("%s: kaynak %d bayt, yazılan %d bayt" % (ad, boyut, len(veri)))
    prebuilt = os.path.join(work, "prebuilt")
    satirlar = []
    if os.path.isdir(prebuilt):
        for d in sorted(os.listdir(prebuilt)):
            if os.path.isdir(os.path.join(prebuilt, d)) and not os.path.islink(os.path.join(prebuilt, d)):
                satirlar += ["prebuilt/%s/%s" % (d, e) for e in sorted(os.listdir(os.path.join(prebuilt, d)))]
    # .tmp/ üst düzeyi (yalnız ad + lstat türü; içerik ve boyut yok, alt dizinlere inilmez): verify-lock hükmünden bağımsız toplanır, böylece
    # TMP_UST_SONRA tablosu yanlışsa derleme yeniden koşmadan artifact'tan görülür (SK-951-6).
    tmp_yol = os.path.join(work, ".tmp")
    tmp_tur = tur_adi(tmp_yol)
    tmp_satirlar = [".tmp: %s" % ("yok" if tmp_tur == "okunamadı" else "dizin değil (%s)" % tmp_tur)]
    if tmp_tur == TUR_DIZIN:
        try:
            tmp_satirlar = ["%s\t%s" % (tur_adi(os.path.join(tmp_yol, ad)), ascii(ad)[1:-1]) for ad in sorted(os.listdir(tmp_yol))]
        except OSError:
            tmp_satirlar = [".tmp: listelenemedi"]
    _tani_yaz(os.path.join(out, "tmp-liste.txt"), ("\n".join(tmp_satirlar) + "\n").encode("utf-8"))
    _tani_yaz(os.path.join(out, "prebuilt-liste.txt"), ("\n".join(satirlar) + "\n").encode("utf-8"))
    _tani_yaz(os.path.join(out, "indeks.txt"), ("\n".join(indeks) + "\n").encode("utf-8"))
    print("tani: %d dosya, %d bayt yazıldı: %s" % (len(indeks), toplam, out))
    return 0


# MARK: - check

RE_KOSU = re.compile(rb"[\x20-\x7e]{16,65536}")
RE_LISANS = re.compile(rb"libavutil license: ([\x20-\x7e]{1,100})(?:\x00|\Z)")
CONFIG_CAPA = b"--target-os="


RE_TIRNAKSIZ = re.compile(r"[A-Za-z0-9_/.+-]*")   # configure sh_quote'un tırnaklamadığı küme


def belirtec_tirnakla(t):
    """FFmpeg configure'ın bir argümanı FFMPEG_CONFIGURATION'a katma biçimi (configure@946fcce :4555-4560 ve sh_quote :615-619):
    ilk '=' e kadarki kısım olduğu gibi, değer kısmı tek tırnak '\\'' ile kaçırılıp küme dışı karakter varsa tek tırnağa alınır."""
    if "=" in t:
        ad, deger = t.split("=", 1)
        ad += "="
    else:
        ad, deger = "", t
    deger = deger.replace("'", "'\\''")
    if RE_TIRNAKSIZ.fullmatch(deger) is None:
        deger = "'" + deger + "'"
    return ad + deger


def belirtec_ayir(ham):
    """Ham configure dizesini belirteçlere ayırır. Ayırma tırnak kurallarıyla yapılır (virgüllü ya da boşluklu değer tek tırnaklıdır) ve
    belirteçlerin yeniden tırnaklanıp boşlukla birleşimi ham dizeye bayt bayt eşit olmalıdır: tırnaksız/çift tırnaklı/çift boşluklu
    biçim FFmpeg'in yazabileceği biçim değildir ve sessizce kabul edilmez."""
    lex = shlex.shlex(ham, posix=True)
    lex.commenters = ""          # varsayılan '#' yorum karakteri bir değerde '#'ten sonrasını sessizce düşürürdü
    lex.whitespace_split = True
    try:
        belirtecler = list(lex)
    except ValueError as e:
        raise Kirmizi("configure dizesi ayrıştırılamadı (kapanmamış tırnak ya da kaçış): %s" % gizle(str(e)))
    yeniden = " ".join(belirtec_tirnakla(t) for t in belirtecler)
    if yeniden != ham:
        i = next((n for n, (a, b) in enumerate(zip(yeniden, ham)) if a != b), min(len(yeniden), len(ham)))
        baslangic = max(0, i - 8)
        raise Kirmizi("configure dizesi FFmpeg'in tırnaklama biçiminde değil (ilk fark %d. bayt: %s)" % (i, gizle(ham[baslangic:baslangic + 40])))
    return belirtecler


def yapilandirma_dizeleri(veri):
    """libavutil baytlarında --target-os= çapalı configure dizeleri ((ham dize, belirteç listesi) çiftleri) ve lisans dizeleri."""
    dizeler = []
    for m in RE_KOSU.finditer(veri):
        s = m.group(0)
        if CONFIG_CAPA in s:
            ham = s.decode("ascii")
            dizeler.append((ham, belirtec_ayir(ham)))
    lisanslar = sorted(set(m.group(1).decode("ascii") for m in RE_LISANS.finditer(veri)))
    return dizeler, lisanslar


def guvenlik_bayrak_sorunlari(belirtecler):
    """V15 güvenlik bayrağı kuralı (SK-893-4, SK-927-5): tam belirteç eşitliği ve değer denetimi. Boş liste = geçer."""
    sorun = []
    for f in GUVENLIK_BAYRAKLARI:
        if f not in belirtecler:
            sorun.append("%s yok" % f)
    adlar = [t.split("=", 1)[0] for t in belirtecler]
    protokol = [t for t, a in zip(belirtecler, adlar) if a in ("--enable-protocol", "--enable-protocols")]
    if protokol != ["--enable-protocol=file"]:
        sorun.append("--enable-protocol/--enable-protocols belirteçleri tam olarak [--enable-protocol=file] değil")
    for y in YASAK_ENABLE:
        if y in adlar:
            sorun.append("%s belirteci var" % y)
    for t in belirtecler:
        for onek in YASAK_ONEKLER:
            if t.startswith(onek):
                sorun.append("%s önekli belirteç var" % onek)
                break
    if "--enable-protocol=file" in belirtecler and "--disable-protocols" in belirtecler:
        if max(i for i, t in enumerate(belirtecler) if t == "--disable-protocols") > belirtecler.index("--enable-protocol=file"):
            sorun.append("--disable-protocols, --enable-protocol=file'dan önce gelmiyor")
    return sorun


def configure_denetle(belirtecler, lisanslar):
    sorun = list(guvenlik_bayrak_sorunlari(belirtecler))
    for z in ZORUNLU_BAYRAKLAR:
        if z not in belirtecler:
            sorun.append("%s yok" % z)
    for g in GPL_BAYRAKLARI:
        if g in belirtecler:
            sorun.append("%s var" % g)
    for t in belirtecler:
        ad = t.split("=", 1)[0]
        if ad.startswith("--enable-lib") and ad not in HARICI_KITAPLIKLAR:
            sorun.append("izin verilmeyen harici kütüphane: %s" % ad)
    if len(lisanslar) != 1 or not lisanslar[0].startswith("LGPL"):
        sorun.append("libavutil license dizesi tek ve LGPL ile başlayan değil")
    return sorun


def otool_ciktilari(ikili, calistir):
    out = []
    for arg in ("-L", "-l"):
        p = calistir(["otool", arg, ikili], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        kirmizi_if(p.returncode != 0, "otool %s başarısız: %s" % (arg, os.path.basename(ikili)))
        out.append(p.stdout.decode("utf-8", "replace"))
    return out


def yol_izinli(p):
    return p.startswith("/usr/lib/") or p.startswith("/System/Library/") or p.startswith("@rpath/")


def otool_sorunlari(cikti_L, cikti_l):
    """Yalnız /usr/lib/, /System/Library/ ve @rpath/ bağımlılıkları; rpath ve kimlik ana makine yolu taşımaz (SK-490-4)."""
    sorun = []
    for satir in cikti_L.splitlines()[1:]:
        s = satir.strip()
        if not s:
            continue
        p = s.split(" (compatibility", 1)[0].strip()
        if not yol_izinli(p):
            sorun.append("otool -L izinli olmayan yol: %s" % p[:80])
    komut = None
    for satir in cikti_l.splitlines():
        s = satir.strip()
        if s.startswith("cmd "):
            komut = s[4:].strip()
            continue
        if komut == "LC_RPATH" and s.startswith("path "):
            p = s[5:].split(" (offset", 1)[0].strip()
            if not p.startswith("@"):
                sorun.append("LC_RPATH ana makine yolu: %s" % p[:80])
        elif komut == "LC_ID_DYLIB" and s.startswith("name "):
            p = s[5:].split(" (offset", 1)[0].strip()
            if not p.startswith("@rpath/"):
                sorun.append("LC_ID_DYLIB @rpath/ değil: %s" % p[:80])
        elif komut in ("LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB", "LC_LOAD_UPWARD_DYLIB") and s.startswith("name "):
            p = s[5:].split(" (offset", 1)[0].strip()
            if not yol_izinli(p):
                sorun.append("%s izinli olmayan yol: %s" % (komut, p[:80]))
    return sorun


RE_BROTLI = re.compile(rb"brotli", re.IGNORECASE)


def brotli_sorunlari(cikti_L, veri):
    """FreeType brotli'siz derlenir (patches/05); hiçbir ikili brotli'ye bağlanmaz ve brotli sembolü ya da dizesi taşımaz.
    otool -L satırları ve ikilinin baytları (sembol tablosu dizeleri dahil) taranır."""
    sorun = []
    for satir in cikti_L.splitlines()[1:]:
        if "brotli" in satir.lower():
            sorun.append("otool -L brotli bağımlılığı: %s" % satir.strip()[:80])
    if RE_BROTLI.search(veri) is not None:
        sorun.append("ikili brotli sembolü ya da dizesi içeriyor")
    return sorun


def dilim_denetle(xc):
    sorun = []
    for fw in CERCEVELER:
        kok = os.path.join(xc, fw + ".xcframework")
        if not os.path.isdir(kok):
            sorun.append("%s.xcframework yok" % fw)
            continue
        dizinler = sorted(d for d in os.listdir(kok) if os.path.isdir(os.path.join(kok, d)))
        if dizinler != sorted(DILIMLER):
            sorun.append("%s.xcframework dilimleri beklenen iki dilim değil" % fw)
    beklenen = sorted(fw + ".xcframework" for fw in CERCEVELER)
    gercek = sorted(d for d in os.listdir(xc) if d.endswith(".xcframework"))
    if gercek != beklenen:
        sorun.append("xcframework kümesi beklenen sekiz değil")
    return sorun


def ikili_yolu(xc, fw, dilim):
    return os.path.join(xc, fw + ".xcframework", dilim, fw + ".framework", fw)


# FFmpeg configure'ın ürettiği derlenmiş küme listeleri (kaynak ağaçta): (tür, dosya, izinli sonekler, yapı adı, dizi adı).
# Yapı ve dizi adları configure@946fcce print_enabled_components çağrılarından (:8961-8969) sabitlenmiştir.
KUME_DOSYALARI = (
    ("protocol", "libavformat/protocol_list.c", ("_protocol",), "URLProtocol", "url_protocols"),
    ("demuxer", "libavformat/demuxer_list.c", ("_demuxer",), "FFInputFormat", "demuxer_list"),
    ("muxer", "libavformat/muxer_list.c", ("_muxer",), "FFOutputFormat", "muxer_list"),
    ("codec", "libavcodec/codec_list.c", ("_decoder", "_encoder"), "FFCodec", "codec_list"),
    ("parser", "libavcodec/parser_list.c", ("_parser",), "FFCodecParser", "parser_list"),
    ("bsf", "libavcodec/bsf_list.c", ("_bsf",), "FFBitStreamFilter", "bitstream_filters"),
    ("filter", "libavfilter/filter_list.c", (), "FFFilter", "filter_list"),
    ("indev", "libavdevice/indev_list.c", ("_demuxer",), "FFInputFormat", "indev_list"),
    ("outdev", "libavdevice/outdev_list.c", ("_muxer",), "FFOutputFormat", "outdev_list"),
)
# Next iOS'ta tek aday outdev'i (audiotoolbox) kapatır (scripts/apple/ffmpeg.sh), --disable-autodetect altında başkası açılmaz:
# outdev listesi gerçekten boştur ve boş olması beklenir; başka hiçbir liste boş olamaz.
BOS_BEKLENEN_TUR = "outdev"
RE_KUME_ADIM = re.compile(rb"    &ff_([A-Za-z0-9_]+),\n")
RE_KUME_GOVDE = re.compile(rb"(?:    &ff_[A-Za-z0-9_]+,\n)*")
KUME_SON = b"    NULL };\n"


def kume_dosyasi_ayristir(icerik, yapi, dizi, dosya):
    """Üretecin dilbilgisiyle bayt düzeyinde sıkı ayrıştırma: `static const <yapı> * const <dizi>[] = {`, sıfır ya da daha çok
    `    &ff_<ad>,` satırı, `    NULL };` ve dosya sonu. Başka satır, CR, ikinci NULL, eksik sonlandırıcı ya da yinelenen girdi kırmızıdır."""
    baslik = ("static const %s * const %s[] = {\n" % (yapi, dizi)).encode("ascii")
    kirmizi_if(len(icerik) < len(baslik) + len(KUME_SON) or not icerik.startswith(baslik), "%s: başlık beklenen biçimde değil" % dosya)
    kirmizi_if(not icerik.endswith(KUME_SON), "%s: NULL sonlandırıcısı beklenen biçimde değil" % dosya)
    govde = icerik[len(baslik):len(icerik) - len(KUME_SON)]
    kirmizi_if(RE_KUME_GOVDE.fullmatch(govde) is None, "%s: beklenmeyen satır (yalnız `    &ff_<ad>,` satırları ve son `    NULL };` olabilir)" % dosya)
    adlar = [m.decode("ascii") for m in RE_KUME_ADIM.findall(govde)]
    kirmizi_if(len(set(adlar)) != len(adlar), "%s: yinelenen girdi" % dosya)
    return adlar


def derlenmis_kume(ffmpeg_agaci):
    """Listelerden (tür, ad) kümesi. Biçim değişmişse, tanınmayan sonek, beklenmedik boş liste ya da dolu outdev kırmızıdır; sessiz geçilmez."""
    kume = set()
    for tur, dosya, sonekler, yapi, dizi in KUME_DOSYALARI:
        yol = os.path.join(ffmpeg_agaci, *dosya.split("/"))
        kirmizi_if(not os.path.isfile(yol), "derlenmiş küme listesi yok: %s" % dosya)
        with open(yol, "rb") as f:
            icerik = f.read()
        bulunan = kume_dosyasi_ayristir(icerik, yapi, dizi, dosya)
        if tur == BOS_BEKLENEN_TUR:
            kirmizi_if(bulunan, "%s kümesi boş değil: %s" % (tur, dosya))
            continue
        kirmizi_if(not bulunan, "derlenmiş küme listesi boş: %s" % dosya)
        for tam in bulunan:
            if not sonekler:
                kume.add((tur, tam))
                continue
            for sonek in sonekler:
                if tam.endswith(sonek) and len(tam) > len(sonek):
                    ad = tam[:-len(sonek)]
                    kume.add((sonek[1:] if tur == "codec" else tur, ad))
                    break
            else:
                raise Kirmizi("%s: tanınmayan girdi soneki" % dosya)
    return kume


def kume_sorunlari(kume):
    """Giriş ve çıkış protokolleri tam {file}; hls, dash, concat, image2 demuxer ve tee muxer yok (bağlayıcı 7)."""
    sorun = []
    protokoller = set(n for t, n in kume if t == "protocol")
    if protokoller != set(["file"]):
        sorun.append("protokol kümesi tam {file} değil")
    for d in YASAK_DEMUXER:
        if ("demuxer", d) in kume:
            sorun.append("%s demuxer'ı derlenmiş" % d)
    if ("muxer", "tee") in kume:
        sorun.append("tee muxer'ı derlenmiş")
    return sorun


def kume_metni(kume, etiket):
    satirlar = ["# forge-ffmpeg derlenmiş küme (%s)" % etiket,
                "# FFmpeg kaynak ağacında configure'ın ürettiği listelerden okundu; her satır: <tür> <ad>"]
    satirlar += ["%s %s" % (t, n) for t, n in sorted(kume)]
    return ("\n".join(satirlar) + "\n").encode("utf-8")


def check(xc, ffmpeg_agaci, otool_calistir=subprocess.run):
    """Kapalı başarısız denetim. (configure satırları, derlenmiş küme) döndürür."""
    sorunlar = dilim_denetle(xc)
    kirmizi_if(sorunlar, "check: dilim/çerçeve düzeni:\n  - " + "\n  - ".join(sorunlar))
    for fw in CERCEVELER:
        for d in DILIMLER:
            ikili = ikili_yolu(xc, fw, d)
            if not os.path.isfile(ikili):
                sorunlar.append("ikili yok: %s/%s" % (fw, d))
                continue
            cikti_L, cikti_l = otool_ciktilari(ikili, otool_calistir)
            with open(ikili, "rb") as f:
                veri = f.read()
            for s in otool_sorunlari(cikti_L, cikti_l) + brotli_sorunlari(cikti_L, veri):
                sorunlar.append("%s/%s: %s" % (fw, d, s))
    satirlar = []
    for d in DILIMLER:
        yol = ikili_yolu(xc, "libavutil", d)
        if not os.path.isfile(yol):
            continue
        with open(yol, "rb") as f:
            veri = f.read()
        try:
            dizeler, lisanslar = yapilandirma_dizeleri(veri)
        except Kirmizi as e:
            sorunlar.append("libavutil/%s: %s" % (d, e))
            continue
        if not dizeler:
            sorunlar.append("libavutil/%s: configure dizesi bulunamadı" % d)
        for ham, dz in dizeler:
            for s in configure_denetle(dz, lisanslar):
                sorunlar.append("libavutil/%s: %s" % (d, s))
            satirlar.append((d, ham))
        if not dizeler and (len(lisanslar) != 1 or not lisanslar[0].startswith("LGPL")):
            sorunlar.append("libavutil/%s: license dizesi tek ve LGPL ile başlayan değil" % d)
    kume = set()
    try:
        kume = derlenmis_kume(ffmpeg_agaci)
        sorunlar += kume_sorunlari(kume)
    except Kirmizi as e:
        sorunlar.append(str(e))
    kirmizi_if(sorunlar, "check başarısız:\n  - " + "\n  - ".join(sorunlar[:40]))
    print("check: %d çerçeve × 2 dilim otool/configure/küme denetimi tamam (%d küme girdisi)" % (len(CERCEVELER), len(kume)))
    return satirlar, kume


RE_FT_DEBUG = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+FT_DEBUG_LOGGING\b", re.MULTILINE)


def ft_debug_olcumu(work, env, satirlar):
    """dlg ikiliye girmez (SK-936-6): FT_DEBUG_LOGGING kapalıyken dlgwrap.c boş derlenir. Kapalı olduğu sabit metinle değil
    ölçümle gösterilir: ftoption.h'de etkin #define, derleme ortamında, FFmpeg configure satırlarında ya da Next betiklerinde
    (FreeType configure satırı scripts/apple/freetype.sh'tadır) geçmesi kırmızıdır. configure-lines.txt'e girecek ölçüm satırını döndürür."""
    yol = os.path.join(agac_yolu(work, "freetype"), "include", "freetype", "config", "ftoption.h")
    kirmizi_if(not os.path.isfile(yol), "freetype: ftoption.h yok; FT_DEBUG_LOGGING ölçülemedi")
    with open(yol, "rb") as f:
        veri = f.read()
    kirmizi_if(RE_FT_DEBUG.search(veri) is not None, "freetype: ftoption.h içinde FT_DEBUG_LOGGING etkin tanımlı (dlg ikiliye girerdi)")
    bulunan = ["derleme ortamı: %s" % a for a, v in sorted(env.items()) if "FT_DEBUG_LOGGING" in a or "FT_DEBUG_LOGGING" in v]
    bulunan += ["FFmpeg configure satırı (%s)" % d for d, satir in satirlar if "FT_DEBUG_LOGGING" in satir]
    betik_sayisi = 0
    for kok, _, dosyalar in os.walk(os.path.join(work, "scripts")):
        for ad in sorted(dosyalar):
            betik_sayisi += 1
            with open(os.path.join(kok, ad), "rb") as f:
                if b"FT_DEBUG_LOGGING" in f.read():
                    bulunan.append("Next betiği: %s" % os.path.relpath(os.path.join(kok, ad), work).replace(os.sep, "/"))
    kirmizi_if(betik_sayisi == 0, "Next scripts/ dizini yok ya da boş; FT_DEBUG_LOGGING ölçülemedi")
    kirmizi_if(bulunan, "FT_DEBUG_LOGGING geçiyor: " + "; ".join(bulunan[:10]))
    return ("freetype-olcum", "ftoption.h FT_DEBUG_LOGGING etkin tanım içermiyor (sha256 %s); derleme ortamında, FFmpeg configure satırlarında ve Next scripts/ altındaki %d dosyada geçmiyor"
            % (hashlib.sha256(veri).hexdigest()[:12], betik_sayisi))


RE_FT_BROTLI = re.compile(rb"^[ \t]*#[ \t]*define[ \t]+FT_CONFIG_OPTION_USE_BROTLI", re.MULTILINE)
RE_PC_REQUIRES_PRIVATE = re.compile(rb"[ \t]*Requires\.private[ \t]*:")


def freetype_brotli_olcumu(work):
    """Brotli yokluğunu olumlu ölçer (SK-938-3); ikili baytlarındaki olumsuz tarama (brotli_sorunlari) tek başına yetmez.
    (a) Next'in yazdığı prebuilt/*/pkgconfig/freetype2.pc (yama 06): en az bir tane, Requires.private satırı tam bir kez ve
    `Requires.private: zlib`; prebuilt/ altındaki hiçbir .pc brotli içermez. (b) Kurulan prebuilt/*/freetype/include/freetype2/
    freetype/config/ftoption.h (yama 05; configure çıktısı, ağaçtaki kopya değil): en az bir tane, FT_CONFIG_OPTION_USE_BROTLI
    etkin tanımlı değil. Dosya yoksa kırmızıdır, atlanmaz. configure-lines.txt'e girecek tek ölçüm satırını döndürür."""
    kok = os.path.join(work, "prebuilt")
    kirmizi_if(not os.path.isdir(kok), "prebuilt/ yok; FreeType brotli yokluğu ölçülemedi")
    dilimler = sorted(os.listdir(kok))
    pc_ler, hlar = [], []
    for d in dilimler:
        yol = os.path.join(kok, d, "pkgconfig", "freetype2.pc")
        if not os.path.isfile(yol):
            continue
        with open(yol, "rb") as f:
            veri = f.read()
        satirlar = [x.rstrip(b"\r") for x in veri.split(b"\n") if RE_PC_REQUIRES_PRIVATE.match(x)]
        kirmizi_if(len(satirlar) != 1, "prebuilt/%s/pkgconfig/freetype2.pc: Requires.private satırı %d kez (tam bir kez olmalı)" % (d, len(satirlar)))
        kirmizi_if(satirlar[0] != b"Requires.private: zlib",
                   "prebuilt/%s/pkgconfig/freetype2.pc: Requires.private yalnız zlib değil: %s" % (d, satirlar[0].decode("utf-8", "replace")[:80]))
        pc_ler.append((d, hashlib.sha256(veri).hexdigest()[:12]))
    kirmizi_if(not pc_ler, "prebuilt/*/pkgconfig/freetype2.pc bulunamadı; yama 06'nın etkisi ölçülemedi")
    brotlu, pc_sayisi = [], 0
    for yol_, _, dosyalar in os.walk(kok):
        for ad in sorted(dosyalar):
            if ad.lower().endswith(".pc"):
                pc_sayisi += 1
                with open(os.path.join(yol_, ad), "rb") as f:
                    if b"brotli" in f.read().lower():
                        brotlu.append(os.path.relpath(os.path.join(yol_, ad), work).replace(os.sep, "/"))
    kirmizi_if(brotlu, "prebuilt/ altında brotli geçen .pc: " + "; ".join(brotlu[:10]))
    for d in dilimler:
        yol = os.path.join(kok, d, "freetype", "include", "freetype2", "freetype", "config", "ftoption.h")
        if not os.path.isfile(yol):
            continue
        with open(yol, "rb") as f:
            veri = f.read()
        kirmizi_if(RE_FT_BROTLI.search(veri) is not None, "prebuilt/%s: kurulan ftoption.h içinde FT_CONFIG_OPTION_USE_BROTLI etkin tanımlı" % d)
        hlar.append((d, hashlib.sha256(veri).hexdigest()[:12]))
    kirmizi_if(not hlar, "prebuilt/*/freetype/include/freetype2/freetype/config/ftoption.h bulunamadı; yama 05'in etkisi ölçülemedi")
    return ("freetype-brotli-olcum",
            "freetype2.pc Requires.private yalnız zlib (%s); prebuilt/ altındaki %d .pc dosyasında brotli yok; kurulan ftoption.h FT_CONFIG_OPTION_USE_BROTLI etkin tanım içermiyor (%s)"
            % (", ".join("%s sha256 %s" % x for x in pc_ler), pc_sayisi, ", ".join("%s sha256 %s" % x for x in hlar)))


def check_tam(work, xc):
    """check + FreeType ölçümleri: (configure satırları ve ölçüm satırları, küme)."""
    satirlar, kume = check(xc, agac_yolu(work, "ffmpeg"))
    satirlar.append(ft_debug_olcumu(work, derleme_env(), satirlar))
    satirlar.append(freetype_brotli_olcumu(work))
    return satirlar, kume


# MARK: - package

def guvenli_ac(zip_yolu, hedef, toplam_sinir=ZIP_TOPLAM_SINIR, dosya_sinir=ZIP_DOSYA_SINIR):
    """Zip'i yeni bir dizine açar; güvensiz girdi kırmızıdır (SK-893-11 kuralları). {yol: (sha256, boyut, calistirilabilir)} döner."""
    kirmizi_if(os.path.lexists(hedef), "açma dizini zaten var")
    os.makedirs(hedef)
    sonuc = {}
    toplam = 0
    katlanmis = set()
    with zipfile.ZipFile(zip_yolu) as z:
        for zi in z.infolist():
            ad = zi.filename
            kirmizi_if(not ad or ad.startswith("/") or "\\" in ad or ":" in ad or "\0" in ad, "zip girdi adı güvensiz")
            if zi.is_dir():
                continue
            parcalar = ad.split("/")
            for p in parcalar:
                kirmizi_if(p in ("", ".", "..") or p.endswith(".") or p.endswith(" "), "zip girdi adı güvensiz bileşen içeriyor")
                kirmizi_if(p.split(".")[0].lower() in WINDOWS_AYRILMIS, "zip girdi adı ayrılmış aygıt adı içeriyor")
            kirmizi_if(YOL_KARAKTER_RE.fullmatch(ad) is None, "zip girdi adı yazdırılabilir ASCII dışında ya da boşluk içeriyor")
            kirmizi_if(ad.lower() in katlanmis, "zip içinde büyük-küçük harf katlamasında çakışan girdi")
            katlanmis.add(ad.lower())
            mod = (zi.external_attr >> 16) & 0o170000
            kirmizi_if(mod == 0o120000, "zip içinde sembolik bağ var")
            kirmizi_if(zi.file_size > dosya_sinir, "zip girdisi boyut sınırını aşıyor")
            toplam += zi.file_size
            kirmizi_if(toplam > toplam_sinir, "zip toplam boyut sınırını aşıyor")
            veri = z.read(zi)
            kirmizi_if(len(veri) != zi.file_size, "zip girdi boyutu başlıkla uyuşmuyor")
            yol = os.path.join(hedef, *parcalar)
            os.makedirs(os.path.dirname(yol), exist_ok=True)
            with open(yol, "wb") as f:
                f.write(veri)
            calistirilabilir = bool((zi.external_attr >> 16) & 0o111)
            if os.name == "posix":
                os.chmod(yol, 0o755 if calistirilabilir else 0o644)
            sonuc[ad] = (hashlib.sha256(veri).hexdigest(), len(veri), calistirilabilir)
    kirmizi_if(not sonuc, "zip boş")
    return sonuc


def manifest_baytlari(commit, dosyalar):
    nesne = {"commit": commit, "files": dict((p, {"sha256": h}) for p, h in dosyalar.items()),
             "kind": MANIFEST_KIND, "schema": 1, "source": NEXT_REPO}
    return (json.dumps(nesne, indent=2, sort_keys=True) + "\n").encode("utf-8")


def liste_baytlari(commit, dosyalar):
    satirlar = LISTE_BASLIK + ["# repo: " + NEXT_REPO, "# commit: " + commit, LISTE_SON_BASLIK]
    for p in sorted(dosyalar, key=lambda x: x.encode("utf-8")):
        satirlar.append("%s  %s" % (dosyalar[p], p))
    return ("\n".join(satirlar) + "\n").encode("utf-8")


def zip_yaz(kaynak_kok, xc_adi, hedef_zip):
    """Bir xcframework'ü kip bitleri korunarak, sıralı ve sabit zaman damgasıyla zip'ler."""
    girdiler = []
    for kok, dizinler, dosyalar in os.walk(os.path.join(kaynak_kok, xc_adi)):
        dizinler.sort()
        for d in dizinler:
            kirmizi_if(os.path.islink(os.path.join(kok, d)), "sembolik bağ var: %s" % d)
        for ad in sorted(dosyalar):
            tam = os.path.join(kok, ad)
            kirmizi_if(os.path.islink(tam), "sembolik bağ var: %s" % ad)
            girdiler.append(tam)
    with zipfile.ZipFile(hedef_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for tam in sorted(girdiler):
            goreli = os.path.relpath(tam, kaynak_kok).replace(os.sep, "/")
            kirmizi_if(YOL_KARAKTER_RE.fullmatch(goreli) is None, "yol yazdırılabilir ASCII dışında ya da boşluk içeriyor: %s" % goreli[:80])
            zi = zipfile.ZipInfo(goreli, date_time=(1980, 1, 1, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            calistirilabilir = os.name == "posix" and bool(os.stat(tam).st_mode & 0o111)
            zi.external_attr = (0o100755 if calistirilabilir else 0o100644) << 16
            with open(tam, "rb") as f:
                z.writestr(zi, f.read())


def sha256_dosya(yol):
    h = hashlib.sha256()
    with open(yol, "rb") as f:
        for blok in iter(lambda: f.read(1 << 20), b""):
            h.update(blok)
    return h.hexdigest()


def arac_kaydi(rapor_yamalar, etiket, calistir_satir=calistir_satir):
    """toolchain.txt: SK-888-2 alanları. Ham günlük ve ortam dökümü girmez; yalnız herkese açık koşu kimliği alanları."""
    env = temiz_env()
    sat = ["# forge-ffmpeg toolchain (%s)" % etiket]
    xcb = subprocess.run(["xcodebuild", "-version"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120) if shutil.which("xcodebuild") else None
    sat.append("xcodebuild: " + (" / ".join(xcb.stdout.decode("utf-8", "replace").split("\n")[:2]) if xcb is not None else "okunamadı"))
    sat.append("xcode-select -p: " + calistir_satir(["xcode-select", "-p"], env))
    sat.append("python3: " + sys.version.split()[0])
    sat.append("git: " + calistir_satir(["git", "--version"]))
    sat.append("brew: " + calistir_satir(["brew", "--version"], env))
    # gas-preprocessor.pl `#!/usr/bin/env perl` ile açılır: PATH'teki ilk perl çalışır. Sürümü bu satırlar kaydeder (ilk koşuya kadar ölçülmedi).
    sat.append("perl (PATH'te ilk: %s): %s" % (shutil.which("perl", path=env["PATH"]) or "yok", calistir_satir(["perl", "-v"], env)))
    sat.append("/usr/bin/perl: " + calistir_satir(["/usr/bin/perl", "-v"], env))
    sat.append("gas-preprocessor.pl (yerleştirilen) sha256: " + GAS_SON_SHA256)
    for a in ("autoconf", "automake", "glibtool", "pkg-config", "cmake", "meson", "ninja", "nasm", "yasm", "gperf", "bison", "makeinfo", "gsed"):
        sat.append("%s: %s" % (a, calistir_satir([a, "--version"], env)))
    for anahtar in ("ImageOS", "ImageVersion", "GITHUB_RUN_ID", "GITHUB_SHA", "GITHUB_REF"):
        if os.environ.get(anahtar):
            sat.append("%s: %s" % (anahtar, os.environ[anahtar]))
    for ad, yerel in (("build.py", os.path.join(HERE, "build.py")), ("lock.json", os.path.join(HERE, "lock.json"))):
        if os.path.isfile(yerel):
            sat.append("%s sha256: %s" % (ad, sha256_dosya(yerel)))
    sat.append("yamalar:")
    sat += ["  " + r for r in rapor_yamalar] or ["  (yok)"]
    return ("\n".join(sat) + "\n").encode("utf-8")


def kaynak_duzenlemeleri(work, kilit):
    """Next'in derleme sırasında kaynaklarda yaptığı ve bizim yamalarımızın izlenen dosya farkı (SK-893-10)."""
    parcalar = []
    for k in kilit["sources"]:
        yol = agac_yolu(work, k["name"])
        out = git_ok(["diff", "--no-ext-diff", "--no-textconv", "--no-color"], yol)
        parcalar.append(("### %s %s %s\n" % (k["name"], k["ref"], k["commit"])).encode("utf-8") + out + (b"\n" if out and not out.endswith(b"\n") else b""))
    return b"".join(parcalar)


def paketle(xc, cikti, etiket, kilit, work, rapor_yamalar, satirlar, kume):
    """Çerçeveleri zip'ler, zip'leri yeni bir dizine açıp manifest ve listeyi o ağaçtan üretir; SHA256SUMS yazar."""
    kirmizi_if(not etiket_gecerli(etiket), "etiket biçimi geçersiz")
    next_commit = kaynak_haritasi(kilit)[NEXT_ADI]["commit"]
    os.makedirs(cikti, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="paket-", dir=os.path.dirname(os.path.abspath(cikti)))
    try:
        sahne = os.path.join(tmp, "sahne")
        shutil.copytree(xc, sahne, symlinks=True)
        silinen = 0
        for kok, dizinler, dosyalar in os.walk(sahne):
            for ad in list(dosyalar):
                if ad in PAKETTEN_SILINEN and os.path.basename(kok).endswith(".framework"):
                    os.remove(os.path.join(kok, ad))
                    silinen += 1
        for kok, dizinler, dosyalar in os.walk(sahne):
            kirmizi_if(any(a in PAKETTEN_SILINEN and os.path.basename(kok).endswith(".framework") for a in dosyalar), "silinmesi gereken dosya kaldı")
        zipler = []
        for fw in CERCEVELER:
            ad = "%s.xcframework.zip" % fw
            zip_yaz(sahne, fw + ".xcframework", os.path.join(cikti, ad))
            zipler.append(ad)
        acik = os.path.join(tmp, "acik")
        os.makedirs(acik)
        dosyalar = {}
        for ad in zipler:
            sonuc = guvenli_ac(os.path.join(cikti, ad), os.path.join(acik, ad[:-4]))
            for yol, (h, _, _) in sonuc.items():
                kirmizi_if(yol in dosyalar, "iki zip aynı yolu taşıyor")
                dosyalar[yol] = h
        # Manifest ve liste açılmış ağaçtan üretilir (SK-888-4): zip içeriği ile liste aynı baytlardan gelir.
        manifest = manifest_baytlari(next_commit, dosyalar)
        liste = liste_baytlari(next_commit, dosyalar)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    yazilacak = [
        ("lgpl-manifest.json", manifest),
        ("frameworks.sha256", liste),
        ("source-lock.json", kilit["bytes"]),
        ("compiled-set.txt", kume_metni(kume, etiket)),
        ("configure-lines.txt", ("# forge-ffmpeg configure satırları (%s)\n" % etiket + "".join("%s: %s\n" % (d, s) for d, s in satirlar)).encode("utf-8")),
        ("toolchain.txt", arac_kaydi(rapor_yamalar, etiket)),
        ("source-edits.diff", kaynak_duzenlemeleri(work, kilit)),
    ]
    for ad, veri in yazilacak:
        with open(os.path.join(cikti, ad), "wb") as f:
            f.write(veri)
    print("package: %d dosya silindi (strip-frameworks.sh/SOURCE), %d zip, manifest %d girdi" % (silinen, len(zipler), len(dosyalar)))
    return sha256sums_yaz(cikti)


def sha256sums_yaz(cikti):
    adlar = sorted(a for a in os.listdir(cikti) if a != "SHA256SUMS")
    for a in adlar:
        kirmizi_if(DIST_AD_RE.fullmatch(a) is None or not os.path.isfile(os.path.join(cikti, a)), "dist dosya adı geçersiz: %s" % a)
    veri = "".join("%s  %s\n" % (sha256_dosya(os.path.join(cikti, a)), a) for a in adlar)
    with open(os.path.join(cikti, "SHA256SUMS"), "wb") as f:
        f.write(veri.encode("utf-8"))
    ozet = hashlib.sha256(veri.encode("utf-8")).hexdigest()
    print("SHA256SUMS_SHA256=" + ozet)
    return ozet


# MARK: - Komut satırı

def hepsi(a):
    kilit = kilit_oku(a.lock)
    kirmizi_if(not etiket_gecerli(a.label), "label v9.0.2-forge.N ya da deneme-<7 hex> biçiminde olmalı")
    kirmizi_if(os.path.lexists(a.out), "çıktı dizini zaten var")
    araclari_denetle()
    fetch(a.work, kilit)
    kilit_dogrula(a.work, kilit, "once")
    arsiv_yaz(a.work, kilit, a.label, a.out, v15_dosya_listesi())
    rapor = yama_uygula(a.work, os.path.join(HERE, "patches"))
    rapor.append(libpng_olcumu(a.work))
    xc = derle(a.work)
    kilit_dogrula(a.work, kilit, "sonra")
    satirlar, kume = check_tam(a.work, xc)
    paketle(xc, a.out, a.label, kilit, a.work, rapor, satirlar, kume)
    return 0


def asama_tools(a):
    araclari_denetle()
    return 0


def asama_fetch(a):
    fetch(a.work, kilit_oku(a.lock))
    return 0


def asama_verify(a):
    kilit_dogrula(a.work, kilit_oku(a.lock, dosya_izni=a.allow_file), a.asama, a.allow_file)
    return 0


def asama_archive(a):
    arsiv_yaz(a.work, kilit_oku(a.lock), a.label, a.out, v15_dosya_listesi())
    return 0


def asama_patch(a):
    yama_uygula(a.work, os.path.join(HERE, "patches"))
    print("patch: " + libpng_olcumu(a.work))
    return 0


def asama_build(a):
    print("build çıktısı: " + derle(a.work))
    return 0


def asama_tani(a):
    return tani_topla(a.work, a.out)


def asama_check(a):
    check_tam(a.work, cikti_dizini_bul(a.work))
    return 0


def asama_package(a):
    kilit = kilit_oku(a.lock)
    xc = cikti_dizini_bul(a.work)
    satirlar, kume = check_tam(a.work, xc)
    paketle(xc, a.out, a.label, kilit, a.work, [], satirlar, kume)
    return 0


def asama_lock_durum(a):
    kilit = kilit_oku(a.lock, siki=False)
    for k in kilit["sources"]:
        print("%-16s %-10s %s" % (k["name"], k["ref"], k["commit"] or "DOĞRULANMADI (commit yok)"))
    if kilit["unverified"]:
        print("lock-durum: %d kaynağın commit'i doğrulanmadı; derleme başlamaz" % len(kilit["unverified"]), file=sys.stderr)
        return 1
    return 0


def asama_selftest(a):
    return selftest()


def ayristirici():
    ap = argparse.ArgumentParser(prog="build.py", description="Forge FFmpeg (V15) derleme betiği; ayrıntı için dosya başlığına bakın.")
    alt = ap.add_subparsers(dest="komut")

    def ekle(ad, islev, work=False, out=False, label=False, lock=True):
        p = alt.add_parser(ad)
        if work:
            p.add_argument("--work", required=True, help="çalışma dizini (fetch için var olmamalı)")
        if out:
            p.add_argument("--out", required=True, help="çıktı dizini (yeni)")
        if label:
            p.add_argument("--label", required=True, help="v9.0.2-forge.N (etiket koşusu) ya da deneme-<7 hex>")
        if lock:
            p.add_argument("--lock", default=os.path.join(HERE, "lock.json"))
        p.set_defaults(islev=islev)
        return p

    ekle("all", hepsi, work=True, out=True, label=True)
    ekle("tools", asama_tools, lock=False)
    ekle("fetch", asama_fetch, work=True)
    pv = ekle("verify-lock", asama_verify, work=True)
    pv.add_argument("--asama", choices=["once", "sonra"], required=True)
    pv.add_argument("--allow-file", action="store_true", help="yalnız yerel sınama: file:// adreslerine izin ver")
    ekle("archive", asama_archive, work=True, out=True, label=True)
    ekle("patch", asama_patch, work=True, lock=False)
    ekle("build", asama_build, work=True, lock=False)
    ekle("tani", asama_tani, work=True, out=True, lock=False)
    ekle("check", asama_check, work=True, lock=False)
    ekle("package", asama_package, work=True, out=True, label=True)
    ekle("lock-durum", asama_lock_durum)
    ekle("selftest", asama_selftest, lock=False)
    return ap


def main(argv=None):
    # Windows konsolları (cp1252) Türkçe karakterleri yazamaz; çıktı kaybolmasın, çökmesin.
    for akis in (sys.stdout, sys.stderr):
        if hasattr(akis, "reconfigure"):
            akis.reconfigure(errors="replace")
    ap = ayristirici()
    a = ap.parse_args(argv)
    if not getattr(a, "komut", None):
        ap.print_help()
        return 2
    try:
        return a.islev(a)
    except Kirmizi as e:
        print("build.py: KIRMIZI: %s" % e, file=sys.stderr)
        return 1


# MARK: - selftest

class Sinama(object):
    def __init__(self):
        self.sayi = 0
        self.basarisiz = []

    def tamam(self, ad, kosul):
        self.sayi += 1
        if not kosul:
            self.basarisiz.append(ad)
            print("  KIRMIZI: %s" % ad)

    def kirmizi_olmali(self, ad, islev, parca=None):
        """İşlev Kirmizi atmalı (ve varsa mesaj parçasını içermeli); atmazsa sınama kırmızı."""
        self.sayi += 1
        try:
            islev()
        except Kirmizi as e:
            if parca is not None and parca not in str(e):
                self.basarisiz.append(ad + " (yanlış neden: %s)" % str(e)[:120])
                print("  KIRMIZI: %s (yanlış neden: %s)" % (ad, str(e)[:120]))
            return
        self.basarisiz.append(ad + " (kırmızı vermedi)")
        print("  KIRMIZI: %s (kırmızı vermedi)" % ad)


def _sil(yol):
    def hata(islev, p, _):
        os.chmod(p, stat.S_IWRITE)
        islev(p)
    shutil.rmtree(yol, onerror=hata)


def _yaz(yol, veri):
    os.makedirs(os.path.dirname(yol), exist_ok=True)
    with open(yol, "wb") as f:
        f.write(veri)


def _bag_sil(yol):
    try:
        os.remove(yol)
    except OSError:
        os.rmdir(yol)       # Windows'ta dizin bağı rmdir ile silinir


def _sonra_dogrula(work, kilit):
    """Selftest: Next'in derleme sırasında .tmp/'ye bıraktığı dizinleri (yoksa) ekleyip `sonra` kipini koşar, eklediklerini geri alır;
    sahte derleme çıktısı olmayan çalışma ağaçlarında eski `sonra` sınamaları kendi nedenleriyle kırmızı/yeşil kalsın diye."""
    eklenen = []
    try:
        for ad, tur in TMP_UST_SONRA.items():
            yol = os.path.join(work, ".tmp", ad)
            if tur == TUR_DIZIN and not os.path.lexists(yol):
                os.makedirs(yol)
                eklenen.append(yol)
        kilit_dogrula(work, kilit, "sonra", True)
    finally:
        for yol in eklenen:
            shutil.rmtree(yol, ignore_errors=True)


def _selftest_tmp_kumesi(s, kok, work, kilit):
    """.tmp/ üst düzey kümesi ve türleri (SK-951-1…3, 5, 6). work: fetch edilmiş, gas dosyası yerinde, .tmp/ içinde yalnız source ve gas var."""
    tmp = os.path.join(work, ".tmp")
    sonra_adlari = [a for a in TMP_UST_SONRA if a not in TMP_UST_ONCE]
    s.tamam("TMP_UST_ONCE: elle yazılmış tam küme {source, gas-preprocessor.pl}", sorted(TMP_UST_ONCE) == ["gas-preprocessor.pl", "source"])
    s.tamam("TMP_UST_SONRA: elle yazılmış tam beş ad (sürüm 16.1)",
            sorted(TMP_UST_SONRA) == sorted(["source", "gas-preprocessor.pl", "cmake", "bundle-apple-universal-iphoneos-16.1", "bundle-apple-universal-iphonesimulator-16.1"]))
    s.tamam("bundle adlarındaki sürüm IOS_SECENEKLERI'ndeki --target= değerinden türer",
            IOS_HEDEF_SURUMU == "16.1" and ["--target=" + IOS_HEDEF_SURUMU] == [a for a in IOS_SECENEKLERI if a.startswith("--target=")]
            and all(a.endswith("-" + IOS_HEDEF_SURUMU) for a in sonra_adlari if a.startswith("bundle-")))
    s.tamam("TMP_UST türleri: gas dosya, diğerleri dizin", TMP_UST_SONRA[GAS_DOSYASI] == TUR_DOSYA and all(t == TUR_DIZIN for a, t in TMP_UST_SONRA.items() if a != GAS_DOSYASI))
    bag_hedef = os.path.join(kok, "tmp-bag-hedef")
    os.makedirs(bag_hedef, exist_ok=True)

    def baglan(yol):
        try:
            os.symlink(bag_hedef, yol, target_is_directory=True)
            return True
        except (OSError, NotImplementedError):
            return False            # Windows'ta ayrıcalık yoksa bağ kurulamaz; POSIX koşularında bu sınamalar çalışır

    def kur(*adlar):
        for ad in adlar:
            os.makedirs(os.path.join(tmp, ad))

    def kaldir(*adlar):
        for ad in adlar:
            yol = os.path.join(tmp, ad)
            if os.path.islink(yol):
                _bag_sil(yol)
            elif os.path.isfile(yol):
                os.remove(yol)
            elif os.path.isdir(yol):
                shutil.rmtree(yol)

    def iki_kip(ad, parca):
        for asama in ("once", "sonra"):
            s.kirmizi_olmali("%s (%s)" % (ad, asama), lambda: kilit_dogrula(work, kilit, asama, True), parca)

    # tam küme yeşil: derleme öncesi {source, gas}; derleme sonrası bu ikisi ve üç Next çıktısı
    kilit_dogrula(work, kilit, "once", True)
    kur(*sonra_adlari)
    kilit_dogrula(work, kilit, "sonra", True)
    s.tamam("tmp kümesi: once tam iki girdiyle, sonra tam beş girdiyle yeşil", True)
    # fazladan girdi: sonra'da ve once'ta kırmızı; ileti adı ve türü yazar
    sonra_fazla = [("fazla dizin", "fazla-cikti", TUR_DIZIN), ("ffkitprotocols dizini (--no-ffmpeg-kit-protocols varken oluşmaz)", "ffkitprotocols", TUR_DIZIN),
                   ("mac-catalyst bundle'ı (--arch iki dilim)", "bundle-apple-universal-mac-catalyst-" + IOS_HEDEF_SURUMU, TUR_DIZIN),
                   ("düz ios bundle'ı (--xcframework olmayan dal)", "bundle-apple-universal-ios-" + IOS_HEDEF_SURUMU, TUR_DIZIN),
                   ("başka sürümlü bundle (önek/joker eşleşmesi yok)", "bundle-apple-universal-iphoneos-9.9", TUR_DIZIN),
                   ("nokta ile başlayan dosya", ".gizli", TUR_DOSYA)]
    for ad, girdi, tur in sonra_fazla:
        yol = os.path.join(tmp, girdi)
        if tur == TUR_DIZIN:
            os.makedirs(yol)
        else:
            _yaz(yol, b"x")
        s.kirmizi_olmali("sonra: fazladan girdi: %s" % ad, lambda: kilit_dogrula(work, kilit, "sonra", True), ".tmp/%s kilitte yok (%s)" % (girdi, tur))
        kaldir(girdi)
    if baglan(os.path.join(tmp, "fazla-bag")):
        s.kirmizi_olmali("sonra: fazladan girdi: bağ (türü bağ yazılır)", lambda: kilit_dogrula(work, kilit, "sonra", True), ".tmp/fazla-bag kilitte yok (bağ)")
        kaldir("fazla-bag")
    kilit_dogrula(work, kilit, "sonra", True)
    # Next çıktıları derleme öncesi kümede yok: once kipinde her biri fazla girdidir
    kaldir(*sonra_adlari)
    for girdi in sonra_adlari:
        kur(girdi)
        s.kirmizi_olmali("once: derleme sonrası girdi derleme öncesi kümede yok: %s" % girdi, lambda: kilit_dogrula(work, kilit, "once", True), ".tmp/%s kilitte yok (dizin)" % girdi)
        kaldir(girdi)
    kilit_dogrula(work, kilit, "once", True)
    kur(*sonra_adlari)
    # tür: dosya ya da bağ, dizin olması gereken girdilerde kırmızı
    for girdi in sonra_adlari:
        yol = os.path.join(tmp, girdi)
        kaldir(girdi)
        _yaz(yol, b"x")
        s.kirmizi_olmali("sonra: %s dosya" % girdi, lambda: kilit_dogrula(work, kilit, "sonra", True), ".tmp/%s gerçek dizin değil (dosya)" % girdi)
        kaldir(girdi)
        if baglan(yol):
            s.kirmizi_olmali("sonra: %s bağ (hedef gerçek bir dizin olsa da)" % girdi, lambda: kilit_dogrula(work, kilit, "sonra", True), ".tmp/%s gerçek dizin değil (bağ)" % girdi)
            kaldir(girdi)
        s.kirmizi_olmali("sonra: %s eksik" % girdi, lambda: kilit_dogrula(work, kilit, "sonra", True), ".tmp/%s eksik (dizin olmalı)" % girdi)
        kur(girdi)
    kilit_dogrula(work, kilit, "sonra", True)
    # source dizini iki kipte gerçek dizin olmalı (bağ ya da dosya kırmızı)
    kaldir(*sonra_adlari)
    kaynak_yedek = os.path.join(kok, "tmp-source-yedek")
    os.rename(os.path.join(tmp, "source"), kaynak_yedek)
    try:
        _yaz(os.path.join(tmp, "source"), b"x")
        iki_kip("tmp kümesi: source dosya", ".tmp/source gerçek dizin değil (dosya)")
        os.remove(os.path.join(tmp, "source"))
        if baglan(os.path.join(tmp, "source")):
            iki_kip("tmp kümesi: source bağ", ".tmp/source gerçek dizin değil (bağ)")
            _bag_sil(os.path.join(tmp, "source"))
        iki_kip("tmp kümesi: source eksik", ".tmp/source eksik (dizin olmalı)")
    finally:
        os.rename(kaynak_yedek, os.path.join(tmp, "source"))
    kilit_dogrula(work, kilit, "once", True)
    # .tmp kendisi: yok, dosya, bağ (hedefi dolu bir dizin olsa da); iki kipte kırmızı (isdir korumalı atlama yok, SK-951-3)
    tmp_yedek = os.path.join(kok, "tmp-yedek")
    os.rename(tmp, tmp_yedek)
    try:
        iki_kip("tmp kümesi: .tmp yok", ".tmp yok")
        _yaz(tmp, b"x")
        iki_kip("tmp kümesi: .tmp dosya", ".tmp gerçek dizin değil (dosya)")
        os.remove(tmp)
        try:
            os.symlink(tmp_yedek, tmp, target_is_directory=True)
            baglandi = True
        except (OSError, NotImplementedError):
            baglandi = False
        if baglandi:
            iki_kip("tmp kümesi: .tmp bir dizine bağ", ".tmp gerçek dizin değil (bağ)")
            _bag_sil(tmp)
    finally:
        os.rename(tmp_yedek, tmp)
    kilit_dogrula(work, kilit, "once", True)
    s.tamam("tmp kümesi: sınamalardan sonra durum geri yüklendi (once yeşil)", sorted(os.listdir(tmp)) == ["gas-preprocessor.pl", "source"])
    # ileti ve ad kaçışı: satır sonu içeren ad günlüğe ham yazılmaz
    kaza = "a\nb"
    try:
        _yaz(os.path.join(tmp, kaza), b"x")
    except OSError:
        kaza = None
    if kaza is not None:
        try:
            kilit_dogrula(work, kilit, "once", True)
            s.tamam("tmp kümesi: satır sonlu ad kırmızı", False)
        except Kirmizi as e:
            s.tamam("tmp kümesi: satır sonlu ad kaçışlı yazılır (ham satır sonu yok)", "a\\nb" in str(e) and "a\nb" not in str(e))
        kaldir(kaza)


def _git_deposu(yol, dosyalar, etiket=None):
    """Sahte kaynak: git plumbing ile (dosya sistemi kipi gerekmeden) tek commit. dosyalar: {yol: (bayt, kip)}. commit SHA döner."""
    os.makedirs(yol)
    git_ok(["init", "-q"], yol)
    for p, (veri, mod) in sorted(dosyalar.items()):
        if mod == "160000":
            sha = veri      # gitlink: nesne depoda yoktur, yalnız SHA yazılır
        else:
            sha = git_ok(["hash-object", "-w", "--stdin"], yol, girdi=veri).decode().strip()
        git_ok(["update-index", "--add", "--cacheinfo", "%s,%s,%s" % (mod, sha, p)], yol)
    agac = git_metin(["write-tree"], yol)
    env = dict(os.environ, GIT_AUTHOR_NAME="s", GIT_AUTHOR_EMAIL="s@s", GIT_COMMITTER_NAME="s", GIT_COMMITTER_EMAIL="s@s",
               GIT_AUTHOR_DATE="2026-01-01T00:00:00Z", GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
    p = subprocess.run(["git", "commit-tree", agac, "-m", "x"], cwd=yol, env=env, stdout=subprocess.PIPE, check=True)
    commit = p.stdout.decode().strip()
    git_ok(["update-ref", "refs/heads/main", commit], yol)
    git_ok(["symbolic-ref", "HEAD", "refs/heads/main"], yol)
    if etiket:
        git_ok(["tag", etiket, commit], yol)
    return commit


# FreeType'ın gerçek .gitmodules içeriği (blob SHA'sı GITMODULES_FREETYPE; selftest bunu hash-object ile doğrular).
FREETYPE_GITMODULES_ICERIK = b'[submodule "dlg"]\n\tpath = subprojects/dlg\n\turl = https://github.com/nyorain/dlg.git\n'


# Sahte gas-preprocessor.pl: gerçek betiğin dönüşüme giren iki Perl düzenli ifade parçasını (her biri tam bir kez) ve GPL başlığını taşır.
GAS_SAHTE = (b"#!/usr/bin/env perl\n# This code is licensed under GPLv2 or later\n"
             b"my $a = qr/(?:ld|st)\\d\\s+({\\s*v(\\d+)\\s*})/;\n")


def _sahte_kilit(kok, bozuk=None):
    """On beş sahte kaynak depo + onlara file:// ile işaret eden kilit. bozuk: {ad: {yol: (bayt, kip) ya da None (sil)}}.
    Gerçek yerleşim taklit edilir: freetype'ta dlg gitlink'i + gerçek .gitmodules, opus'ta boş .gitmodules.
    dlg önce yapılır: freetype'ın gitlink'i onun commit'ine işaret eder."""
    kaynaklar = {}
    sunucu = os.path.join(kok, "sunucu")
    for ad in ["dlg"] + [a for a in KAYNAK_ADLARI if a != "dlg"]:
        dosyalar = {"README": (("kaynak " + ad + "\n").encode(), "100644"), "bin/arac": (b"#!/bin/sh\n", "100755"),
                    "baglanti": (b"README", "120000"), ".gitattributes": (b"README export-ignore\n", "100644")}
        if ad == "dlg":
            dosyalar.update({"LICENSE": (b"Boost Software License - Version 1.0\n", "100644"),
                             "include/dlg/dlg.h": (b"#define DLG 1\n", "100644"), "include/dlg/output.h": (b"#define OUT 1\n", "100644"),
                             "src/dlg/dlg.c": (b"int dlg;\n", "100644")})
        if ad == "freetype":
            dosyalar.update({".gitmodules": (FREETYPE_GITMODULES_ICERIK, "100644"), "subprojects/dlg": (kaynaklar["dlg"]["commit"], "160000")})
        if ad == "opus":
            dosyalar[".gitmodules"] = (b"", "100644")
        if ad == GAS_ADI:
            dosyalar["gas-preprocessor.pl"] = (GAS_SAHTE, "100755")
        for p, v in (bozuk or {}).get(ad, {}).items():
            if v is None:
                dosyalar.pop(p, None)
            else:
                dosyalar[p] = v
        yol = os.path.join(sunucu, ad)
        commit = _git_deposu(yol, dosyalar, etiket="s-" + ad)
        kaynaklar[ad] = {"name": ad, "url": "file:///" + yol.replace("\\", "/").lstrip("/"), "ref": "s-" + ad,
                         "version": "1", "commit": commit}
        if ad == GAS_ADI:
            kaynaklar[ad]["sha256"] = hashlib.sha256(GAS_SAHTE).hexdigest()
    return {"sources": [kaynaklar[a] for a in KAYNAK_ADLARI], "unverified": [], "bytes": b"{}\n"}


def _kilit_dosyasi(yol, kilit):
    _yaz(yol, (json.dumps({"schema": 1, "sources": kilit["sources"]}, indent=2) + "\n").encode("utf-8"))


def _otool_sahte(dep_l=None, rpath="@loader_path/Frameworks", kimlik="@rpath/x.framework/x"):
    dep_l = dep_l or ["/usr/lib/libz.1.dylib", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"]

    def calistir(komut, **kw):
        sonuc = subprocess.CompletedProcess(komut, 0)
        if komut[1] == "-L":
            sonuc.stdout = ("%s:\n" % komut[2] + "".join("\t%s (compatibility version 1.0.0, current version 1.0.0)\n" % d for d in dep_l)).encode()
        else:
            blok = ["Load command 1\n          cmd LC_ID_DYLIB\n      cmdsize 48\n         name %s (offset 24)\n" % kimlik,
                    "Load command 2\n          cmd LC_RPATH\n      cmdsize 48\n         path %s (offset 12)\n" % rpath]
            for i, d in enumerate(dep_l):
                blok.append("Load command %d\n          cmd LC_LOAD_DYLIB\n      cmdsize 56\n         name %s (offset 24)\n" % (i + 3, d))
            sonuc.stdout = "".join(blok).encode()
        sonuc.stderr = b""
        return sonuc
    return calistir


TEMEL_CONFIGURE = ("--prefix=/x --enable-version3 --arch=arm64 --target-os=darwin --enable-cross-compile --enable-videotoolbox "
                   "--enable-libvpx --enable-libopus --enable-libass --enable-libfontconfig --enable-libfreetype --enable-libfribidi "
                   "--enable-libharfbuzz --enable-libmp3lame --enable-zlib").split(" ")


def _configure(ekstra=(), cikar=()):
    parcalar = [t for t in TEMEL_CONFIGURE]
    parcalar += [t for t in GUVENLIK_BAYRAKLARI if t not in cikar]
    parcalar += list(ekstra)
    return parcalar


# FFmpeg configure'ın FFMPEG_CONFIGURATION'a yazdığı gerçek biçim, elle yazılmış (build.py'nin tırnaklama işleviyle üretilmez; yoksa sınama
# kendini doğrulardı). Virgüllü değer, boşluklu değer ve değerin içindeki tek tırnak ('\'' dizisi) configure@946fcce sh_quote çıktısıdır.
GERCEK_TEMEL_HAM = ("--prefix=/x --enable-version3 --arch=arm64 --target-os=darwin --enable-cross-compile --enable-videotoolbox "
                    "--enable-libvpx --enable-libopus --enable-libass --enable-libfontconfig --enable-libfreetype --enable-libfribidi "
                    "--enable-libharfbuzz --enable-libmp3lame --enable-zlib "
                    "--disable-network --disable-protocols --enable-protocol=file --disable-demuxer='hls,dash,concat,image2' --disable-muxer=tee")
GERCEK_HAM = ("--prefix=/x --enable-version3 --arch=arm64 --target-os=darwin --enable-cross-compile --enable-videotoolbox "
              "--enable-libvpx --enable-libopus --enable-libass --enable-libfontconfig --enable-libfreetype --enable-libfribidi "
              "--enable-libharfbuzz --enable-libmp3lame --enable-zlib --extra-cflags='-O2 -DX' --extra-cxxflags='-DNAME='\\''x'\\''' "
              "--disable-network --disable-protocols --enable-protocol=file --disable-demuxer='hls,dash,concat,image2' --disable-muxer=tee")


def _ham(belirtecler):
    """Belirteç listesinden ham dize: yalnız virgüllü ya da boşluklu değerler tek tırnaklanır (selftest girdileri bunlarla sınırlı)."""
    parcalar = []
    for t in belirtecler:
        ad, esit, deger = t.partition("=")
        parcalar.append(ad + esit + "'" + deger + "'" if esit and ("," in deger or " " in deger) else t)
    return " ".join(parcalar)


def _sahte_ikili(belirtecler, lisans=b"LGPL version 3 or later"):
    """belirtecler: belirteç listesi ya da ham dize (str)."""
    ham = belirtecler if isinstance(belirtecler, str) else _ham(belirtecler)
    ic = b"\x00\x01libavutil license: " + lisans + b"\x00\x02" + ham.encode() + b"\x00\x03"
    return b"\xcf\xfa\xed\xfe" + ic


def _sahte_xc(kok, belirtecler=None, lisans=b"LGPL version 3 or later", dilimler=DILIMLER, ekstra=None):
    """Sekiz sahte xcframework; libavutil'in ikilisinde configure ve lisans dizesi vardır."""
    belirtecler = belirtecler if belirtecler is not None else _configure()
    xc = os.path.join(kok, "xc")
    for fw in CERCEVELER:
        _yaz(os.path.join(xc, fw + ".xcframework", "Info.plist"), b"<plist/>")
        for d in dilimler:
            icerik = _sahte_ikili(belirtecler, lisans) if fw == "libavutil" else b"\xcf\xfa\xed\xfe" + fw.encode()
            _yaz(ikili_yolu(xc, fw, d), icerik)
        _yaz(os.path.join(xc, fw + ".xcframework", DILIMLER[0], fw + ".framework", "LICENSE"), b"LGPL\n")
    _yaz(os.path.join(xc, "ffmpegkit.xcframework", DILIMLER[0], "ffmpegkit.framework", "strip-frameworks.sh"), b"#!/bin/sh\n")
    _yaz(os.path.join(xc, "ffmpegkit.xcframework", DILIMLER[0], "ffmpegkit.framework", "SOURCE"), b"https://example.invalid/\n")
    if ekstra:
        for p, v in ekstra.items():
            _yaz(os.path.join(xc, *p.split("/")), v)
    return xc


# configure@946fcce :8961-8969 print_enabled_components çağrılarından elle yazılmış (dosya: yapı, dizi); build.py'nin KUME_DOSYALARI'ndan türetilmez.
KUME_GERCEK = {
    "libavfilter/filter_list.c": ("FFFilter", "filter_list"),
    "libavcodec/codec_list.c": ("FFCodec", "codec_list"),
    "libavcodec/parser_list.c": ("FFCodecParser", "parser_list"),
    "libavcodec/bsf_list.c": ("FFBitStreamFilter", "bitstream_filters"),
    "libavformat/demuxer_list.c": ("FFInputFormat", "demuxer_list"),
    "libavformat/muxer_list.c": ("FFOutputFormat", "muxer_list"),
    "libavdevice/indev_list.c": ("FFInputFormat", "indev_list"),
    "libavdevice/outdev_list.c": ("FFOutputFormat", "outdev_list"),
    "libavformat/protocol_list.c": ("URLProtocol", "url_protocols"),
}
# Gerçek yapıdaki örnek içerik: filter_list'in sonuna configure'ın koşulsuz eklediği dört girdi dahil, outdev boş (Next iOS'ta audiotoolbox'ı kapatır).
KUME_ORNEK = {
    "libavformat/protocol_list.c": ["file_protocol"],
    "libavformat/demuxer_list.c": ["mov_demuxer", "matroska_demuxer", "mp3_demuxer"],
    "libavformat/muxer_list.c": ["mp4_muxer", "null_muxer"],
    "libavcodec/codec_list.c": ["h264_decoder", "aac_encoder"],
    "libavcodec/parser_list.c": ["h264_parser"],
    "libavcodec/bsf_list.c": ["null_bsf"],
    "libavfilter/filter_list.c": ["vf_scale", "af_aresample", "asrc_abuffer", "vsrc_buffer", "asink_abuffer", "vsink_buffer"],
    "libavdevice/indev_list.c": ["lavfi_demuxer", "avfoundation_demuxer"],
    "libavdevice/outdev_list.c": [],
}


def _liste_dosyasi(dosya, girdiler, yapi=None):
    """configure'ın print_enabled_components çıktısı: başlık, `    &ff_<ad>,` satırları, `    NULL };`."""
    yapi_adi, dizi = KUME_GERCEK[dosya]
    return "static const %s * const %s[] = {\n" % (yapi or yapi_adi, dizi) + "".join("    &ff_%s,\n" % g for g in girdiler) + "    NULL };\n"


def _sahte_kume(kok, degistir=None, atla=()):
    """degistir: dosya -> girdi listesi (tam, gerçek biçimli dosya üretilir) ya da str (ham içerik, bozuk biçim denemeleri için)."""
    ff = os.path.join(kok, "ffmpeg")
    for dosya, girdiler in KUME_ORNEK.items():
        if dosya in atla:
            continue
        icerik = _liste_dosyasi(dosya, girdiler)
        if degistir and dosya in degistir:
            deger = degistir[dosya]
            icerik = deger if isinstance(deger, str) else _liste_dosyasi(dosya, deger)
        _yaz(os.path.join(ff, *dosya.split("/")), icerik.encode())
    return ff


# Next v9.0.2'den (ffmpeg.sh, source.sh) olduğu gibi alınan çapa bölgeleri: gerçek yamalar bunlara uygulanır.
NEXT_FFMPEG_SH = ('  --disable-cuda \\\n  --disable-cuvid \\\n  --disable-nvenc \\\n  --disable-vaapi \\\n  --disable-vdpau \\\n'
                  '  --disable-cuda \\\n  --disable-cuvid \\\n  --disable-nvenc \\\n  --disable-vaapi \\\n  --disable-vdpau \\\n'
                  '  ${CONFIGURE_POSTFIX} 1>>"${BASEDIR}"/build.log 2>&1\n\nif [[ $? -ne 0 ]]; then\n  exit 1\nfi\n')
NEXT_SOURCE_SH = ('  expat)\n    SOURCE_REPO_URL="https://github.com/arthenica/libexpat"\n    SOURCE_ID="R_2_8_1"\n    SOURCE_TYPE="TAG"\n    ;;\n'
                  '  ffmpeg)\n    SOURCE_REPO_URL="https://github.com/arthenica/FFmpeg"\n    SOURCE_ID="n9.0.2"\n    SOURCE_TYPE="TAG"\n    ;;\n')


NEXT_HARFBUZZ_SH = ('"${MESON:-meson}" setup "${BUILD_DIR}" \\\n  --cross-file="$CROSS_FILE" \\\n  --buildtype=release \\\n'
                    '  --default-library=static \\\n  -Db_staticpic=true \\\n')


# Next v9.0.2 scripts/apple/freetype.sh ve arthenica/freetype2 VER-2-14-3 autogen.sh'tan olduğu gibi alınan çapa bölgeleri.
NEXT_FREETYPE_SH = ('./configure \\\n  --prefix="${LIB_INSTALL_PREFIX}" \\\n  --with-pic \\\n  --with-zlib \\\n  --with-png \\\n  --with-brotli=yes \\\n'
                    '  --with-sysroot="${SDK_PATH}" \\\n  --without-harfbuzz \\\n')
FREETYPE_AUTOGEN_SH = ('  if test -d "$DLG_INC_DIR"; then\n    :\n  else\n    echo "Checking out submodule in \\`subprojects/dlg\':"\n'
                       '    git submodule update --init\n  fi\n')
# Next scripts/function-apple.sh@9f04bf1e satır 1415-1434: Next'in elle yazdığı freetype2.pc (Requires.private'ta libbrotlidec).
NEXT_FUNCTION_APPLE_SH = r'''create_freetype_package_config() {
  local FREETYPE_VERSION="$1"

  cat >"${INSTALL_PKG_CONFIG_DIR}/freetype2.pc" <<EOF
prefix=${BASEDIR}/prebuilt/$(get_build_directory)/freetype
exec_prefix=\${prefix}
libdir=\${exec_prefix}/lib
includedir=\${prefix}/include

Name: FreeType 2
URL: https://freetype.org
Description: A free, high-quality, and portable font engine.
Version: ${FREETYPE_VERSION}
Requires: libpng
Requires.private: zlib, libbrotlidec
Libs: -L\${libdir} -lfreetype
Libs.private:
Cflags: -I\${includedir}/freetype2
EOF
}
'''

# Next scripts/main-ios.sh@9f04bf1e satır 49-72: kütüphane derleme döngüsünün başı ve fontconfig, freetype, gnutls, harfbuzz kolları
# (freetype kolu OK_libjxl bekler; libjxl kapalıysa döngü hiç bitmez).
NEXT_MAIN_IOS_SH = r'''while [ ${#enabled_library_list[@]} -gt $completed ]; do
  for library in "${enabled_library_list[@]}"; do
    let run=0
    case $library in
    fontconfig)
      if [[ $OK_expat -eq 1 ]] && [[ $OK_freetype -eq 1 ]]; then
        run=1
      fi
      ;;
    freetype)
      if [[ $OK_libpng -eq 1 ]] && [[ $OK_libjxl -eq 1 ]]; then
        run=1
      fi
      ;;
    gnutls)
      if [[ $OK_nettle -eq 1 ]] && [[ $OK_gmp -eq 1 ]]; then
        run=1
      fi
      ;;
    harfbuzz)
      if [[ $OK_freetype -eq 1 ]]; then
        run=1
      fi
      ;;
'''


# Next scripts/function-ios.sh@9f04bf1e satır 371-388 (set_toolchain_paths başı) ve scripts/run-apple.sh@9f04bf1e (tamamı): olduğu gibi,
# gh api ile ham bayt olarak alındı (CR yok). Yama 08 ve 09 bunlara uygulanır; çapaların tam dosyalarda tam bir kez geçtiği ayrıca ölçüldü.
NEXT_FUNCTION_IOS_SH = r'''set_toolchain_paths() {
  # CROSS-COMPILING, SO TOOLS THAT RUN ON THE BUILD MACHINE NEED THEIR OWN
  # COMPILER
  set_host_cc "$(command -v clang)"

  if [ ! -f "${FFMPEG_KIT_TMPDIR}/gas-preprocessor.pl" ]; then
    DOWNLOAD_RESULT=$(download "https://github.com/arthenica/gas-preprocessor/raw/v20210917/gas-preprocessor.pl" "gas-preprocessor.pl" "exit")
    if [[ ${DOWNLOAD_RESULT} -ne 0 ]]; then
      exit 1
    fi
    (chmod +x "${FFMPEG_KIT_TMPDIR}"/gas-preprocessor.pl 1>>"${BASEDIR}"/build.log 2>&1) || return 1

    # patch gas-preprocessor.pl against the following warning
    # Unescaped left brace in regex is deprecated here (and will be fatal in Perl 5.32), passed through in regex; marked by <-- HERE in m/(?:ld|st)\d\s+({ <-- HERE \s*v(\d+)\.(\d[bhsdBHSD])\s*-\s*v(\d+)\.(\d[bhsdBHSD])\s*})/ at /Users/taner/Projects/ffmpeg-kit/.tmp/gas-preprocessor.pl line 1065.
    ${SED_INLINE} 's/s+({/s+(\\{/g;s/s\*})/s*\\})/g' "${FFMPEG_KIT_TMPDIR}"/gas-preprocessor.pl
  fi

  LOCAL_GAS_PREPROCESSOR="${FFMPEG_KIT_TMPDIR}/gas-preprocessor.pl"
'''
NEXT_RUN_APPLE_SH = r'''#!/bin/bash

# ENABLE COMMON FUNCTIONS
source "${BASEDIR}"/scripts/function-"${FFMPEG_KIT_BUILD_TYPE}".sh || return 1

LIB_NAME=$1
ENABLED_LIBRARY_PATH="${LIB_INSTALL_BASE}/${LIB_NAME}"

# PREPARE PATHS & DEFINE ${INSTALL_PKG_CONFIG_DIR}
SCRIPT_PATH="${BASEDIR}/scripts/apple/${LIB_NAME}.sh"
set_toolchain_paths "${LIB_NAME}"

# SET BUILD FLAGS
HOST=$(get_host)
export CFLAGS=$(get_cflags "${LIB_NAME}")
export CXXFLAGS=$(get_cxxflags "${LIB_NAME}")
export LDFLAGS=$(get_ldflags "${LIB_NAME}")
export PKG_CONFIG_LIBDIR="${INSTALL_PKG_CONFIG_DIR}"

echo -e "----------------------------------------------------------------"
echo -e "\nINFO: Building ${LIB_NAME} for ${HOST} with the following environment variables\n"
env
echo -e "----------------------------------------------------------------\n"
echo -e "INFO: System information\n"
echo -e "INFO: $(uname -a)\n"
echo -e "----------------------------------------------------------------\n"

cd "${BASEDIR}"/src/"${LIB_NAME}" || return 1

LIB_INSTALL_PREFIX="${ENABLED_LIBRARY_PATH}"
BUILD_DIR=$(get_cmake_build_directory)
CMAKE_SYSTEM_NAME=$(get_apple_cmake_system_name)

# DELETE THE PREVIOUS BUILD OF THE LIBRARY
rm -rf "${LIB_INSTALL_PREFIX}" || return 1
rm -rf "${BUILD_DIR}" || return 1

# EXECUTE BUILD SCRIPT OF EACH ENABLED LIBRARY
source "${SCRIPT_PATH}"
'''


def _yama_agaclari(kok):
    work = os.path.join(kok, "work")
    _yaz(os.path.join(work, "scripts", "apple", "ffmpeg.sh"), NEXT_FFMPEG_SH.encode())
    _yaz(os.path.join(work, "scripts", "apple", "harfbuzz.sh"), NEXT_HARFBUZZ_SH.encode())
    _yaz(os.path.join(work, "scripts", "apple", "freetype.sh"), NEXT_FREETYPE_SH.encode())
    _yaz(os.path.join(work, "scripts", "source.sh"), NEXT_SOURCE_SH.encode())
    _yaz(os.path.join(work, "src", "freetype", "autogen.sh"), FREETYPE_AUTOGEN_SH.encode())
    _yaz(os.path.join(work, "scripts", "function-apple.sh"), NEXT_FUNCTION_APPLE_SH.encode())
    _yaz(os.path.join(work, "scripts", "main-ios.sh"), NEXT_MAIN_IOS_SH.encode())
    _yaz(os.path.join(work, "scripts", "function-ios.sh"), NEXT_FUNCTION_IOS_SH.encode())
    _yaz(os.path.join(work, "scripts", "run-apple.sh"), NEXT_RUN_APPLE_SH.encode())
    return work


def _yama_json(yol, **ust):
    y = {"id": "t1", "tree": NEXT_ADI, "file": "scripts/source.sh", "op": "replace", "anchor": 'SOURCE_ID="R_2_8_1"',
         "text": 'SOURCE_ID="R_2_9_0"', "project": "p", "license": "LGPL-3.0", "date": "2026-11-08", "notice": "değiştirildi", "reason": "r"}
    y.update(ust)
    for k in [k for k, v in y.items() if v is None]:
        del y[k]
    _yaz(yol, (json.dumps(y) + "\n").encode("utf-8"))


GOLDEN_COMMIT = "9f04bf1e3c97def70b98e198cb80a57b43ef5b45"
GOLDEN_DOSYALAR = {
    "a.xcframework/Info.plist": hashlib.sha256(b"plist").hexdigest(),
    "a.xcframework/ios-arm64/a.framework/a": hashlib.sha256(b"bin").hexdigest(),
    "a.xcframework/ios-arm64-simulator/a.framework/Headers/B.h": hashlib.sha256(b"hdr").hexdigest(),
}
# ffmpeg_pin.py parse_manifest/parse_list(fmt=2)/check_manifest_matches ile bir kez doğrulanmış vektörün SHA-256'sı.
GOLDEN_MANIFEST_SHA256 = "0654de1f65406d8a9fb993651aa3f396de137b31b1aa473872838cf3164d0ac9"
GOLDEN_LISTE_SHA256 = "0110665df8f79c070ee19354e20bb94e9f7f47e2a67b45fc0d8225dafb533a6e"


def _yml_run_blogu(yml, adim_adi):
    """build.yml'deki adımın `run: |` gövdesi (girinti soyulmuş); yalnız biçimi bu dosyadaki gibi olan adımlar için."""
    satirlar = yml.split("\n")
    i = [n for n, l in enumerate(satirlar) if l.strip() == "- name: " + adim_adi]
    kirmizi_if(len(i) != 1, "build.yml: %s adımı tam bir kez bulunmalı" % adim_adi)
    j = [n for n in range(i[0], len(satirlar)) if satirlar[n].strip() == "run: |"][0]
    govde = []
    for l in satirlar[j + 1:]:
        if l.strip() and not l.startswith(" " * 10):
            break
        govde.append(l[10:])
    return "\n".join(govde)


def _yml_adim_sinamalari(s, kok, yml):
    """Workflow adımlarını gerçek bash'te sahte girdilerle koşar (yalnız POSIX): özet adımı (m1) ve release not sürümleri (m2)."""
    t = os.path.join(kok, "yml")
    os.makedirs(os.path.join(t, "dist"))
    try:
        ozet_adimi = _yml_run_blogu(yml, "SHA256SUMS özeti")
        release = _yml_run_blogu(yml, "Release (taslak, sonra yayın)")
        release = release[:release.index("gh release create")]     # gh çağrısı koşulmaz
    except (Kirmizi, ValueError, IndexError) as e:
        s.tamam("build.yml adım gövdeleri ayrıştırılır: %s" % e, False)
        return

    def kos(betik, env_ek):
        p = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", betik], cwd=t, env=dict(os.environ, **env_ek),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        return p.returncode

    def kilit_yaz(ff="9.0.2", nxt="v9.0.2", ffref="n9.0.2"):
        nesne = {"schema": 1, "sources": [{"name": "ffmpeg-kit-next", "ref": nxt, "version": "9.0.2"}, {"name": "ffmpeg", "ref": ffref, "version": ff}]}
        _yaz(os.path.join(t, "dist", "source-lock.json"), json.dumps(nesne).encode())

    notlar = os.path.join(t, "notes.md")
    kilit_yaz()
    rc = kos(release, {"ETIKET": "v9.0.2-forge.1", "RUNNER_TEMP": t})
    s.tamam("release notu: sürümler kilitten", rc == 0 and open(notlar).read().startswith("FFmpegKitNext v9.0.2 + FFmpeg n9.0.2 for the Forge app.\n"))
    kilit_yaz("8.1.3", "v8.1.0", "n8.1.3")
    rc = kos(release, {"ETIKET": "v8.1.3-forge.2", "RUNNER_TEMP": t})
    s.tamam("release notu: yedek sürümde (8.1.3) not doğru sürümü yazar", rc == 0 and "FFmpegKitNext v8.1.0 + FFmpeg n8.1.3 for" in open(notlar).read())
    kilit_yaz()
    s.tamam("release notu: etiket kilitle uyuşmazsa kırmızı", kos(release, {"ETIKET": "v9.0.3-forge.1", "RUNNER_TEMP": t}) != 0)
    s.tamam("release notu: bozuk etiket kırmızı", kos(release, {"ETIKET": "v9.0.2", "RUNNER_TEMP": t}) != 0)
    for kotu in ("v9.0.2-forge.1x", "v9.0.2-forge.1 x", "xv9.0.2-forge.1"):
        s.tamam("release notu: gevşek etiket '%s' kırmızı" % kotu, kos(release, {"ETIKET": kotu, "RUNNER_TEMP": t}) != 0)
    kilit_yaz(ffref="n9.0.2 x")
    s.tamam("release notu: kilitteki biçimsiz ref kırmızı", kos(release, {"ETIKET": "v9.0.2-forge.1", "RUNNER_TEMP": t}) != 0)
    kilit_yaz(nxt="v9.0.2`id`")
    s.tamam("release notu: kilitteki komut benzeri ref kırmızı", kos(release, {"ETIKET": "v9.0.2-forge.1", "RUNNER_TEMP": t}) != 0)
    kilit_yaz()
    os.remove(os.path.join(t, "dist", "source-lock.json"))
    s.tamam("release notu: kilit dosyası yoksa kırmızı", kos(release, {"ETIKET": "v9.0.2-forge.1", "RUNNER_TEMP": t}) != 0)

    cikti = os.path.join(t, "cikti")
    sahte_bin = os.path.join(t, "bin")
    _yaz(os.path.join(t, "dist", "SHA256SUMS"), b"x\n")
    env = {"RUNNER_TEMP": t, "GITHUB_OUTPUT": cikti, "PATH": sahte_bin + os.pathsep + os.environ["PATH"]}

    def sahte_shasum(govde):
        _yaz(os.path.join(sahte_bin, "shasum"), ("#!/bin/bash\n" + govde + "\n").encode())
        os.chmod(os.path.join(sahte_bin, "shasum"), 0o755)
        _yaz(cikti, b"")

    sahte_shasum('echo "%s  dosya"' % ("a" * 64))
    rc = kos(ozet_adimi, env)
    s.tamam("özet adımı: geçerli özet GITHUB_OUTPUT'a yazılır", rc == 0 and open(cikti).read() == "sha256sums=" + "a" * 64 + "\n")
    for ad, govde in (("shasum hata verir", "exit 1"), ("çıktı boş", "true"), ("özet kısa", 'echo "abc  f"'), ("özet büyük harfli", 'echo "%s  f"' % ("A" * 64))):
        sahte_shasum(govde)
        rc = kos(ozet_adimi, env)
        s.tamam("özet adımı: %s kırmızı ve çıktı dosyası boş" % ad, rc != 0 and open(cikti).read() == "")


def selftest():
    global GAS_SON_SHA256
    s = Sinama()
    kok = tempfile.mkdtemp(prefix="forge-ffmpeg-selftest-")
    gercek_gas_son = GAS_SON_SHA256
    # Gerçek sabit önce sabitlenir; sahte kaynak depolar gerçek betiği taşımadığı için aşağıdaki sınamalar sahte betiğin dönüşüm özetiyle koşar.
    s.tamam("GAS_SON_SHA256: gerçek betik (sha256 78ad0635…) + dönüşüm için ölçülen değer",
            gercek_gas_son == "ed085dd43ed95759a2b84afc3b1f9b0b64114b586d0d4b16fbe164b4369ae844")
    GAS_SON_SHA256 = hashlib.sha256(gas_donustur(GAS_SAHTE)).hexdigest()
    try:
        _selftest_icerik(s, kok)
    finally:
        GAS_SON_SHA256 = gercek_gas_son
        try:
            _sil(kok)
        except OSError:
            pass
    if s.basarisiz:
        print("selftest: %d sınamadan %d kırmızı:\n  - %s" % (s.sayi, len(s.basarisiz), "\n  - ".join(s.basarisiz)), file=sys.stderr)
        return 1
    print("selftest: %d sınama tamam" % s.sayi)
    return 0


def _selftest_icerik(s, kok):
    # --- kaynak dosyada assert yok ---
    with open(os.path.join(HERE, "build.py"), "rb") as f:
        agac = ast.parse(f.read())
    s.tamam("build.py'de assert deyimi yok", not any(isinstance(n, ast.Assert) for n in ast.walk(agac)))

    def cagri_sirasi(islev_adi):
        f = [n for n in ast.walk(agac) if isinstance(n, ast.FunctionDef) and n.name == islev_adi][0]
        cagrilar = [c for c in ast.walk(f) if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
        return [c.func.id for c in sorted(cagrilar, key=lambda c: (c.lineno, c.col_offset))]

    sira = cagri_sirasi("hepsi")
    s.tamam("hepsi: libpng ölçümü yamalardan sonra ve derlemeden önce; derlemeden sonra kilit denetimi var",
            sira.index("yama_uygula") < sira.index("libpng_olcumu") < sira.index("derle") < sira.index("kilit_dogrula", sira.index("derle")))
    s.tamam("asama_patch libpng ölçümünü de koşar", "libpng_olcumu" in cagri_sirasi("asama_patch"))

    # --- gerçek lock.json biçimi ---
    try:
        gercek = kilit_oku(os.path.join(HERE, "lock.json"), siki=False)
        s.tamam("lock.json biçimi geçerli (15 kaynak)", len(gercek["sources"]) == 15)
        print("selftest: lock.json'da commit'i doğrulanmamış kaynak: %d" % len(gercek["unverified"]))
        gas = [k for k in gercek["sources"] if k["name"] == GAS_ADI]
        s.tamam("lock.json: gas-preprocessor commit ve blob SHA-256'sı bu turda ölçülen değerler (etiketle değil, SHA'yla kilit)",
                len(gas) == 1 and gas[0]["commit"] == "d09971fad329d32df19f5bbafe88cf2f0ed04ed7"
                and gas[0]["sha256"] == "78ad06351ac8be513fb6e6291d9bb8f667899625c2649093e3b08f59fae950a2"
                and gas[0]["url"] == "https://github.com/arthenica/gas-preprocessor")
    except Kirmizi as e:
        s.tamam("lock.json biçimi geçerli: %s" % e, False)
    for ad, deger in (("etiket", ETIKET_RE), ("deneme", DENEME_RE)):
        s.tamam("%s regex'i" % ad, deger.fullmatch("v9.0.2-forge.1" if ad == "etiket" else "deneme-abc1234") is not None)
    s.tamam("etiket regex'i fazlalığı reddeder", ETIKET_RE.fullmatch("v9.0.2-forge.1x") is None and ETIKET_RE.fullmatch("v9.0.2") is None)

    # --- kilit biçimi (olumsuz) ---
    sahte = _sahte_kilit(os.path.join(kok, "k1"))
    lp = os.path.join(kok, "k1", "lock.json")
    _kilit_dosyasi(lp, sahte)
    s.tamam("sahte kilit geçerli", len(kilit_oku(lp, dosya_izni=True)["sources"]) == 15)
    nesne = json.load(open(lp))

    def varyant(ad, degistir, parca):
        o = json.loads(json.dumps(nesne))
        degistir(o)
        p = os.path.join(kok, "k1", "v-%s.json" % ad)
        _yaz(p, (json.dumps(o) + "\n").encode())
        s.kirmizi_olmali("kilit: " + ad, lambda: kilit_oku(p, dosya_izni=True), parca)

    varyant("büyük harfli commit", lambda o: o["sources"][1].update(commit=o["sources"][1]["commit"].upper()), "40 küçük hex")
    varyant("kısa commit", lambda o: o["sources"][1].update(commit="abc123"), "40 küçük hex")
    varyant("eksik kaynak", lambda o: o["sources"].pop(), "on beş")
    varyant("fazla kaynak", lambda o: o["sources"].append(dict(o["sources"][0], name="fazla")), "on beş")
    varyant("yinelenen kaynak", lambda o: o["sources"].append(dict(o["sources"][0])), "yinelenen")
    varyant("bilinmeyen anahtar", lambda o: o["sources"][0].update(ek="x"), "anahtarlarını")

    def gas_girdisi(o):
        return [k for k in o["sources"] if k["name"] == GAS_ADI][0]

    varyant("gas-preprocessor'da sha256 yok", lambda o: gas_girdisi(o).pop("sha256"), "anahtarlarını")
    varyant("başka kaynakta sha256 var", lambda o: o["sources"][0].update(sha256="a" * 64), "anahtarlarını")
    varyant("gas sha256 büyük harfli", lambda o: gas_girdisi(o).update(sha256=gas_girdisi(o)["sha256"].upper()), "sha256 64")
    varyant("gas sha256 bir hane kısa", lambda o: gas_girdisi(o).update(sha256="a" * 63), "sha256 64")
    varyant("gas sha256 bir hane uzun", lambda o: gas_girdisi(o).update(sha256="a" * 65), "sha256 64")
    varyant("gas sha256 dize değil", lambda o: gas_girdisi(o).update(sha256=None), "sha256 64")
    varyant("commit yok (doğrulanmadı)", lambda o: o["sources"][2].update(commit=None), "doğrulanmamış")
    varyant("url .git soneki", lambda o: o["sources"][0].update(url="https://github.com/arthenica/ffmpeg-kit-next.git"), "url")
    varyant("url http", lambda o: o["sources"][0].update(url="http://github.com/arthenica/ffmpeg-kit-next"), "url")
    varyant("ref boşluklu", lambda o: o["sources"][0].update(ref="v1 2"), "ref")
    p = os.path.join(kok, "k1", "yinelenen.json")
    _yaz(p, b'{"schema": 1, "schema": 1, "sources": []}\n')
    s.kirmizi_olmali("kilit: yinelenen JSON anahtarı", lambda: kilit_oku(p), "yinelenen")

    # --- fetch ve verify-lock ---
    work = os.path.join(kok, "w1")
    fetch(work, sahte, dosya_izni=True)
    kilit_dogrula(work, sahte, "once", True)
    _sonra_dogrula(work, sahte)
    s.tamam("fetch + verify-lock olumlu", True)
    s.kirmizi_olmali("fetch: çalışma dizini var", lambda: fetch(work, sahte, True), "zaten var")
    s.tamam("fetch: Next kökte, bileşen src/ altında, gnu-config .tmp/source/config",
            os.path.isdir(os.path.join(work, ".git")) and os.path.isdir(os.path.join(work, "src", "ffmpeg", ".git"))
            and os.path.isdir(os.path.join(work, ".tmp", "source", "config", ".git"))
            and os.path.isdir(os.path.join(work, ".tmp", "source", GAS_ADI, ".git")))
    # derleme sonrası kirlenme: "sonra" geçer, "once" temiz değil der
    _yaz(os.path.join(work, "src", "ffmpeg", "README"), b"degisti\n")
    _yaz(os.path.join(work, "yeni_dosya"), b"x")
    _sonra_dogrula(work, sahte)
    s.kirmizi_olmali("verify-lock once: kirli ağaç", lambda: kilit_dogrula(work, sahte, "once", True), "temiz değil")
    # kilitte olmayan dizinler
    os.makedirs(os.path.join(work, "src", "rapidjson"))
    s.kirmizi_olmali("verify-lock sonra: src/ içinde kilitte olmayan dizin", lambda: _sonra_dogrula(work, sahte), "kilitte yok")
    shutil.rmtree(os.path.join(work, "src", "rapidjson"))
    os.makedirs(os.path.join(work, ".tmp", "source", "rapidjson"))
    s.kirmizi_olmali("verify-lock sonra: .tmp/source içinde kilitte olmayan dizin", lambda: _sonra_dogrula(work, sahte), "kilitte yok")
    shutil.rmtree(os.path.join(work, ".tmp", "source", "rapidjson"))
    # HEAD kayması
    yol = os.path.join(work, "src", "libvpx")
    git_ok(["-c", "user.name=s", "-c", "user.email=s@s", "commit", "-q", "--allow-empty", "-m", "kayma"], yol)
    s.kirmizi_olmali("verify-lock sonra: HEAD kayması", lambda: _sonra_dogrula(work, sahte), "HEAD")
    s.kirmizi_olmali("verify-lock once: HEAD kayması", lambda: kilit_dogrula(work, sahte, "once", True), "HEAD")
    _sil(os.path.join(work, "src", "libass"))
    s.kirmizi_olmali("verify-lock: ağaç eksik", lambda: _sonra_dogrula(work, sahte), "yok")
    # --- gas-preprocessor.pl: kilitli blob, dönüşüm, ios.sh'tan önce yerleştirme, iki kipte bayt denetimi (SK-947-1…4) ---
    gercek_satir = rb'            if ($line =~ /(?:ld|st)\d\s+({\s*v(\d+)\.(\d[bhsdBHSD])\s*-\s*v(\d+)\.(\d[bhsdBHSD])\s*})/) {' + b"\n"
    beklenen_satir = rb'            if ($line =~ /(?:ld|st)\d\s+(\{\s*v(\d+)\.(\d[bhsdBHSD])\s*-\s*v(\d+)\.(\d[bhsdBHSD])\s*\})/) {' + b"\n"
    s.tamam("gas dönüşümü: Next'in sed'iyle gerçek betiğin 1066. satırında aynı sonucu verir (iki kaçış eklenir)", gas_donustur(gercek_satir) == beklenen_satir)
    s.kirmizi_olmali("gas dönüşümü: parça yoksa kırmızı", lambda: gas_donustur(b"perl\n"), "tam bir kez")
    s.kirmizi_olmali("gas dönüşümü: parça iki kez varsa kırmızı", lambda: gas_donustur(gercek_satir + gercek_satir), "tam bir kez")
    wg = os.path.join(kok, "wg")
    fetch(wg, sahte, True)
    gas_hedef = os.path.join(wg, ".tmp", GAS_DOSYASI)
    with open(gas_hedef, "rb") as f:
        yerlesen = f.read()
    s.tamam("gas: fetch .tmp/gas-preprocessor.pl'i kilitli blob + dönüşüm olarak yerleştirir (kaynak ağaçtaki dosya değişmez)",
            yerlesen == gas_donustur(GAS_SAHTE) and yerlesen != GAS_SAHTE
            and open(os.path.join(wg, ".tmp", "source", GAS_ADI, GAS_DOSYASI), "rb").read() == GAS_SAHTE)
    s.tamam("gas: yerleştirilen dosya normal dosya, bağ değil, POSIX'te 0755",
            stat.S_ISREG(os.lstat(gas_hedef).st_mode) and (os.name != "posix" or stat.S_IMODE(os.lstat(gas_hedef).st_mode) == 0o755))
    kilit_dogrula(wg, sahte, "once", True)
    _sonra_dogrula(wg, sahte)
    s.tamam("gas: verify-lock once ve sonra yerleştirme sonrası yeşil", True)
    s.kirmizi_olmali("gas: yerleştirme var olan yolu ezmez", lambda: gas_yerlestir(wg, sahte), "zaten var")
    gas_yedek = yerlesen
    for ad, bozuk_bayt in (("bir bayt değişti", yerlesen[:-1] + b"X"), ("dönüşümsüz kilitli blob", GAS_SAHTE), ("boş", b"")):
        _yaz(gas_hedef, bozuk_bayt)
        s.kirmizi_olmali("gas: verify-lock once, yerleştirilen dosya %s" % ad, lambda: kilit_dogrula(wg, sahte, "once", True), "baytları")
        s.kirmizi_olmali("gas: verify-lock sonra, yerleştirilen dosya %s (Next ya da derleme değiştirmiş)" % ad, lambda: _sonra_dogrula(wg, sahte), "baytları")
    os.remove(gas_hedef)
    s.kirmizi_olmali("gas: verify-lock once, dosya yok", lambda: kilit_dogrula(wg, sahte, "once", True), "yok")
    s.kirmizi_olmali("gas: verify-lock sonra, dosya yok", lambda: _sonra_dogrula(wg, sahte), "yok")
    try:
        os.symlink(os.path.join(wg, ".tmp", "source", GAS_ADI, GAS_DOSYASI), gas_hedef)
        baglanabildi = True
    except (OSError, NotImplementedError):
        baglanabildi = False       # Windows'ta ayrıcalık yoksa bağ kurulamaz; POSIX koşularında bu sınama çalışır
    if baglanabildi:
        s.kirmizi_olmali("gas: yerleştirilen yol bağ ise verify-lock kırmızı", lambda: _sonra_dogrula(wg, sahte), "normal dosya değil")
        os.remove(gas_hedef)
    _yaz(gas_hedef, gas_yedek)
    os.chmod(gas_hedef, 0o755)
    _sonra_dogrula(wg, sahte)
    if os.name == "posix":
        os.chmod(gas_hedef, 0o644)
        s.kirmizi_olmali("gas: yerleştirilen dosya 0755 değilse verify-lock kırmızı", lambda: _sonra_dogrula(wg, sahte), "0755")
        os.chmod(gas_hedef, 0o755)
    # .tmp/ üst düzey kümesi: derleme öncesi tam {source, gas-preprocessor.pl}
    _yaz(os.path.join(wg, ".tmp", "indirilen.bin"), b"x")
    s.kirmizi_olmali("gas: verify-lock once, .tmp/ altında fazladan dosya (Next indirmesi)", lambda: kilit_dogrula(wg, sahte, "once", True), ".tmp/indirilen.bin kilitte yok")
    os.remove(os.path.join(wg, ".tmp", "indirilen.bin"))
    os.makedirs(os.path.join(wg, ".tmp", "fazla-dizin"))
    s.kirmizi_olmali("gas: verify-lock once, .tmp/ altında fazladan dizin", lambda: kilit_dogrula(wg, sahte, "once", True), ".tmp/fazla-dizin kilitte yok")
    os.rmdir(os.path.join(wg, ".tmp", "fazla-dizin"))
    kilit_dogrula(wg, sahte, "once", True)
    _selftest_tmp_kumesi(s, kok, wg, sahte)
    # kilit/commit tarafı: yanlış baytlı betik, eksik dosya, dönüşüm parçası eksik, dönüşüm sonrası beklenen sabit tutmuyor
    yanlis_gas = _sahte_kilit(os.path.join(kok, "kg1"), {GAS_ADI: {GAS_DOSYASI: (GAS_SAHTE + b"# fazla\n", "100755")}})
    s.kirmizi_olmali("gas: kilitteki sha256 ile commit'teki blob uyuşmazsa fetch kırmızı", lambda: fetch(os.path.join(kok, "wg2"), yanlis_gas, True), "lock.json'daki değil")
    yok_gas = _sahte_kilit(os.path.join(kok, "kg2"), {GAS_ADI: {GAS_DOSYASI: None}})
    s.kirmizi_olmali("gas: commit'te gas-preprocessor.pl yoksa fetch kırmızı", lambda: fetch(os.path.join(kok, "wg3"), yok_gas, True), "bulunamadı")
    sembol_gas = _sahte_kilit(os.path.join(kok, "kg3"), {GAS_ADI: {GAS_DOSYASI: (b"gas-preprocessor.pl", "120000")}})
    s.kirmizi_olmali("gas: commit'te gas-preprocessor.pl sembolik bağsa fetch kırmızı", lambda: fetch(os.path.join(kok, "wg4"), sembol_gas, True), "normal dosya değil")
    for ad, icerik, parca in (("dönüşüm parçası yok", b"#!/usr/bin/env perl\n", "tam bir kez"),
                              ("dönüşüm parçası iki kez", GAS_SAHTE + GAS_SAHTE, "tam bir kez"),
                              ("dönüşüm sonrası sabitle uyuşmaz", GAS_SAHTE + b"# fazla\n", "beklenen sabit")):
        k_gas = _sahte_kilit(os.path.join(kok, "kg-" + ad.replace(" ", "-")), {GAS_ADI: {GAS_DOSYASI: (icerik, "100755")}})
        for k in k_gas["sources"]:
            if k["name"] == GAS_ADI:
                k["sha256"] = hashlib.sha256(icerik).hexdigest()    # kilit commit'teki blob'la tutarlı: kırmızı nedeni yalnız dönüşüm/sabit
        s.kirmizi_olmali("gas: %s -> fetch kırmızı" % ad, lambda: fetch(os.path.join(kok, "wg5"), k_gas, True), parca)
        if os.path.lexists(os.path.join(kok, "wg5")):
            _sil(os.path.join(kok, "wg5"))
    fetch_cagrilari = cagri_sirasi("fetch")
    s.tamam("fetch: gas_yerlestir tüm kaynaklar çekildikten sonra, işlevin sonunda", fetch_cagrilari.index("gas_yerlestir") > fetch_cagrilari.index("kaynak_cek"))
    # build.log indirme bekçisi (SK-947-6), saf işlev: blok sınırı, sayım, benzer ama farklı satırlar
    ilog = os.path.join(kok, "indirme.log")
    ok = True
    for blok in (1, 3, 8, 64):
        for iz in INDIRME_IZLERI:
            for konum in range(0, 40):
                _yaz(ilog, b"x" * konum + iz + b"rest\n" + b"y" * 10)
                ok = ok and indirme_izleri(ilog, blok=blok) == {iz: (1, konum)}
    s.tamam("indirme bekçisi: iki iz her konumda ve blok sınırını aşsa da tam bir kez, doğru konumda bulunur", ok)
    _yaz(ilog, b"a\n" + INDIRME_IZLERI[0] + b"1\n" + INDIRME_IZLERI[0] + b"2\n" + INDIRME_IZLERI[1] + b"\n")
    s.tamam("indirme bekçisi: tekrar eden izler sayılır, ilk konum korunur",
            indirme_izleri(ilog, blok=5) == {INDIRME_IZLERI[0]: (2, 2), INDIRME_IZLERI[1]: (1, 2 + 2 * (len(INDIRME_IZLERI[0]) + 2))})
    for benzer in (b"DEBUG: Downloading custom library x\n", b"INFO: gnu config already downloaded.\n", b"DEBUG: downloaded x\n", b"ERROR: Failed to clone x\n",
                   b"error: failed to download x\n", b"DEBUG: Download skipped\n", b""):
        _yaz(ilog, benzer)
        s.tamam("indirme bekçisi: %r kırmızı vermez" % benzer[:30], indirme_izleri(ilog) == {})
    # alt modül (İ1/İ2) ve eşleşmeyen etiket
    # --- İ1/İ2 istisna tablosu (SK-936-1): altmodul_sorunlari, küçük sahte depolarla ---
    DLG = "395ccad2c1e0daae535c4d20bb0a3f2424648e17"
    ic_k = {"dlg": DLG}
    s.tamam("FreeType .gitmodules sabiti, İ2 blob SHA'sıyla aynı",
            git_ok(["hash-object", "--stdin"], kok, girdi=FREETYPE_GITMODULES_ICERIK).decode().strip() == GITMODULES_FREETYPE.decode())
    s.tamam("boş blob SHA'sı, İ2'nin boş blob'uyla aynı", git_ok(["hash-object", "--stdin"], kok, girdi=b"").decode().strip() == GITMODULES_BOS.decode())
    gm_ft = (FREETYPE_GITMODULES_ICERIK, "100644")
    gl_dlg = (DLG, "160000")
    ic_sayac = [0]

    def ic_dosyalar(ek):
        d = {"README": (b"x\n", "100644")}
        d.update(ek)
        return d

    def ic_dene_unit(ad, agac, ek, parca=None, kilit=None):
        """altmodul_sorunlari küçük bir sahte depoda: parca None ise sorun listesi boş, değilse bir sorun parçayı içermeli."""
        ic_sayac[0] += 1
        d = os.path.join(kok, "ic-%d" % ic_sayac[0])
        commit = _git_deposu(d, ic_dosyalar(ek))
        sorunlar = altmodul_sorunlari(d, commit, agac, ic_k if kilit is None else kilit)
        if parca is None:
            s.tamam(ad + " (sorun yok)", sorunlar == [])
            if sorunlar:
                print("    sorunlar: %s" % sorunlar)
        else:
            s.tamam(ad, any(parca in x for x in sorunlar))
            if not any(parca in x for x in sorunlar):
                print("    sorunlar: %s" % sorunlar)

    ic_dene_unit("(1) opus: kökte boş .gitmodules, gitlink yok", "opus", {".gitmodules": (b"", "100644")})
    ic_dene_unit("(2) freetype: İ1 + FreeType .gitmodules blob'u", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": gl_dlg})
    ic_dene_unit("(3) opus: 1 baytlık .gitmodules", "opus", {".gitmodules": (b"x", "100644")}, ".gitmodules var")
    ic_dene_unit("(4) boş .gitmodules + bir gitlink", "opus", {".gitmodules": (b"", "100644"), "alt": ("a" * 40, "160000")}, ".gitmodules boş ama")
    ic_dene_unit("(4b) boş .gitmodules + bir gitlink: gitlink de kırmızı", "opus", {".gitmodules": (b"", "100644"), "alt": ("a" * 40, "160000")}, "gitlink (alt modül) var")
    ic_dene_unit("(5) gitlink doğru yolda, SHA farklı", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": ("b" * 40, "160000")}, "gitlink (alt modül) var")
    ic_dene_unit("(5b) SHA farklıyken kilitteki dlg için İ1 yok", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": ("b" * 40, "160000")}, "kilitte dlg var ama")
    ic_dene_unit("(6a) doğru SHA, yol subprojects/dlg2", "freetype", {"subprojects/dlg2": gl_dlg}, "gitlink (alt modül) var")
    ic_dene_unit("(6b) doğru SHA, yol Subprojects/dlg (harf farkı)", "freetype", {"Subprojects/dlg": gl_dlg}, "gitlink (alt modül) var")
    # git ağaç girdisinde sondaki / kabul etmez (update-index "Invalid path"); bu yüzden karşılaştırma işlevine doğrudan sorulur.
    s.tamam("(6c) yol sonda / ile: İ1 değil; tam yol: İ1", ic_gitlink_uygun("freetype", b"subprojects/dlg/", DLG, ic_k) is None
            and ic_gitlink_uygun("freetype", b"subprojects/dlg", DLG, ic_k) == "dlg")
    ic_dene_unit("(7) freetype: ikinci gitlink", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": gl_dlg, "subprojects/diger": ("c" * 40, "160000")},
                 "birden fazla gitlink")
    ic_dene_unit("(8) İ1'in aynısı harfbuzz ağacında", "harfbuzz", {"subprojects/dlg": gl_dlg}, "gitlink (alt modül) var")
    ic_dene_unit("(9) alt dizinde .gitmodules (boş blob olsa da)", "opus", {"sub/.gitmodules": (b"", "100644")}, "kökte olmayan")
    ic_dene_unit("(9b) .gitmodules adında dizin içindeki dosya", "opus", {".gitmodules/x": (b"x", "100644")}, "kökte olmayan")
    ic_dene_unit("(9c) farklı harfli .GITMODULES", "opus", {".GITMODULES": (b"", "100644")}, "kökte olmayan")
    ic_dene_unit("(10) FreeType .gitmodules blob'u farklı (url değişmiş)", "freetype",
                 {".gitmodules": (FREETYPE_GITMODULES_ICERIK.replace(b"nyorain", b"baska"), "100644"), "subprojects/dlg": gl_dlg}, "izinli blob değil")
    ic_dene_unit("(11) İ1 var ama kilitte dlg yok", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": gl_dlg}, "gitlink (alt modül) var", kilit={})
    ic_dene_unit("(11b) İ1 var ama kilitte dlg yok: FreeType .gitmodules da kırmızı", "freetype", {".gitmodules": gm_ft, "subprojects/dlg": gl_dlg},
                 "İ1 sağlanmıyor", kilit={})
    ic_dene_unit("FreeType .gitmodules blob'u İ1'siz ağaçta", "opus", {".gitmodules": gm_ft}, "İ1 sağlanmıyor")
    ic_dene_unit("FreeType .gitmodules blob'u harfbuzz'da İ1 ile", "harfbuzz", {".gitmodules": gm_ft, "subprojects/dlg": gl_dlg}, "İ1 sağlanmıyor")
    # .gitmodules sembolik bağı: git update-index bunu reddeder ("cannot add .gitmodules"), git mktree reddetmez; kip denetimi ikinci savunmadır.
    mk = os.path.join(kok, "ic-mk")
    os.makedirs(mk)
    git_ok(["init", "-q"], mk)
    mk_blob = git_ok(["hash-object", "-w", "--stdin"], mk, girdi=b"x").decode().strip()
    mk_agac = git_ok(["mktree"], mk, girdi=("120000 blob %s\t.gitmodules\n" % mk_blob).encode()).decode().strip()
    mk_env = dict(os.environ, GIT_AUTHOR_NAME="s", GIT_AUTHOR_EMAIL="s@s", GIT_COMMITTER_NAME="s", GIT_COMMITTER_EMAIL="s@s")
    mk_commit = subprocess.run(["git", "commit-tree", mk_agac, "-m", "x"], cwd=mk, env=mk_env, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    s.tamam(".gitmodules sembolik bağ (mktree ile): normal dosya değil", any("normal dosya değil" in x for x in altmodul_sorunlari(mk, mk_commit, "opus", ic_k)))
    ic_dene_unit("kilitte dlg var ama freetype ağacında gitlink yok", "freetype", {".gitmodules": (b"", "100644")}, "kilitte dlg var ama")
    ic_dene_unit("gitlink'siz temiz ağaç", "libass", {})
    yanlis = _sahte_kilit(os.path.join(kok, "k4"))
    sunucu_fc = os.path.join(kok, "k4", "sunucu", "fontconfig")
    env_c = dict(os.environ, GIT_AUTHOR_NAME="s", GIT_AUTHOR_EMAIL="s@s", GIT_COMMITTER_NAME="s", GIT_COMMITTER_EMAIL="s@s")
    ikinci = subprocess.run(["git", "commit-tree", yanlis["sources"][3]["commit"] + "^{tree}", "-p", yanlis["sources"][3]["commit"], "-m", "y"],
                            cwd=sunucu_fc, env=env_c, stdout=subprocess.PIPE, check=True).stdout.decode().strip()
    git_ok(["tag", "s-diger", ikinci], sunucu_fc)
    yanlis["sources"][3] = dict(yanlis["sources"][3], ref="s-diger")   # etiket, kilitteki commit'ten farklı bir commit'e işaret eder
    s.kirmizi_olmali("fetch: ref etiketi başka commit'e işaret ediyor", lambda: fetch(os.path.join(kok, "w4"), yanlis, True), "işaret etmiyor")
    yok = _sahte_kilit(os.path.join(kok, "k5"))
    yok["sources"][2] = dict(yok["sources"][2], commit="1" * 40)
    s.kirmizi_olmali("fetch: commit alınamıyorsa etikete dönülmez", lambda: fetch(os.path.join(kok, "w5"), yok, True), "etikete dönülmez")

    # --- iç içe kaynak (dlg) uçtan uca: fetch sırası, gerçek iç içe depo, sonra kipi (SK-936-2, SK-936-5) ---
    w10 = os.path.join(kok, "w10")
    fetch(w10, sahte, True)
    ft_yol, dlg_yol = agac_yolu(w10, "freetype"), agac_yolu(w10, "dlg")
    dlg_kilit_k = [x for x in sahte["sources"] if x["name"] == "dlg"][0]
    dlg_kilit = dlg_kilit_k["commit"]
    s.tamam("agac_yolu: dlg, freetype ağacının subprojects/dlg dizini", os.path.normpath(dlg_yol) == os.path.normpath(os.path.join(w10, "src", "freetype", "subprojects", "dlg")))
    s.tamam("(21) once: dlg gitlink dizininde gerçek iç içe depo ve HEAD gitlink SHA'sı",
            os.path.isdir(os.path.join(dlg_yol, ".git")) and git_metin(["rev-parse", "HEAD"], dlg_yol) == dlg_kilit
            and git_metin(["ls-tree", "HEAD", "subprojects/dlg"], ft_yol).split()[2] == dlg_kilit)
    s.tamam("(21) once: freetype git status --porcelain boş (sahte kilit değil, gerçek iç içe depo)", git_metin(["status", "--porcelain"], ft_yol) == "")
    kilit_dogrula(w10, sahte, "once", True)
    _sonra_dogrula(w10, sahte)
    s.tamam("(1)(2) fetch + once + sonra: opus boş .gitmodules, freetype İ1 ve .gitmodules ile geçer", True)
    s.tamam("dlg ağacı üst düzey src/ altında ayrı dizin açmaz", not os.path.exists(os.path.join(w10, "src", "dlg")))
    ters = dict(sahte, sources=[x for x in sahte["sources"] if x["name"] == "dlg"] + [x for x in sahte["sources"] if x["name"] != "dlg"])
    s.tamam("fetch_sirasi: dlg lock'ta ilk yazılsa da en son", [x["name"] for x in fetch_sirasi(ters)][-1] == "dlg" and ters["sources"][0]["name"] == "dlg")
    w13 = os.path.join(kok, "w13")
    fetch(w13, ters, True)
    kilit_dogrula(w13, ters, "once", True)
    s.tamam("(13) dlg lock'ta freetype'tan önce yazılmış, yine de freetype'tan sonra çekilip doğrulanır",
            git_metin(["rev-parse", "HEAD"], agac_yolu(w13, "dlg")) == dlg_kilit)
    # (22) üst düzeyde src/dlg
    os.makedirs(os.path.join(w10, "src", "dlg"))
    for asama in ("sonra", "once"):
        s.kirmizi_olmali("(22) %s: üst düzeyde src/dlg" % asama, lambda: kilit_dogrula(w10, sahte, asama, True), "src/dlg kilitte yok")
    os.rmdir(os.path.join(w10, "src", "dlg"))
    # (18) sonra: dlg'de değişmiş dosya, sonra dlg HEAD kayması; (23) once: iç içe HEAD gitlink'ten farklı => freetype durumu boş değil
    _yaz(os.path.join(dlg_yol, "README"), b"degisti\n")
    s.kirmizi_olmali("(18) sonra: dlg'de değişmiş dosya", lambda: _sonra_dogrula(w10, sahte), "dlg: ağaç temiz değil")
    git_ok(["checkout", "-q", "--", "README"], dlg_yol)
    _yaz(os.path.join(dlg_yol, "yeni_dosya"), b"x\n")
    s.kirmizi_olmali("(18) sonra: dlg'de izlenmeyen dosya", lambda: _sonra_dogrula(w10, sahte), "dlg: ağaç temiz değil")
    os.remove(os.path.join(dlg_yol, "yeni_dosya"))
    _sonra_dogrula(w10, sahte)
    git_ok(["-c", "user.name=s", "-c", "user.email=s@s", "commit", "-q", "--allow-empty", "-m", "kayma"], dlg_yol)
    s.kirmizi_olmali("(18) sonra: dlg HEAD kayması", lambda: _sonra_dogrula(w10, sahte), "dlg: HEAD kilitteki commit değil")
    s.kirmizi_olmali("(23) once: iç içe HEAD gitlink SHA'sından farklı, freetype durumu boş değil", lambda: kilit_dogrula(w10, sahte, "once", True), "freetype: ağaç temiz değil")
    git_ok(["reset", "-q", "--hard", dlg_kilit], dlg_yol)
    kilit_dogrula(w10, sahte, "once", True)
    if os.name == "posix":
        # iç içe kaynak yolu sembolik bağ ise istisna dışıdır: gitlink yolundaki dizin ağaçta dizin sayılmaz
        yedek = os.path.join(w10, "yedek_dlg")     # subprojects/ dışında: yanlış alarmı (kardeş dizin) karıştırmasın
        os.rename(dlg_yol, yedek)
        os.symlink(yedek, dlg_yol)
        ft_commit = [x for x in sahte["sources"] if x["name"] == "freetype"][0]["commit"]
        s.tamam("iç içe kaynak yolu sembolik bağsa istisna dışı", any("dizin değil" in x for x in agac_ici_sorunlari(ft_yol, ft_commit, "freetype")))
        os.remove(dlg_yol)
        os.rename(yedek, dlg_yol)
        kilit_dogrula(w10, sahte, "once", True)
    # (12) dlg hedef dizini boş değil
    kutu = os.path.join(kok, "w12", "hedef")
    _yaz(os.path.join(kutu, "kalinti"), b"x\n")
    s.kirmizi_olmali("(12) dlg hedef dizini boş değil", lambda: kaynak_cek(dlg_kilit_k, kutu, True), "hedef dizin boş değil")
    # hedef dizin değil: dosya (os.listdir NotADirectoryError atardı) ve boş dizine sembolik bağ (git init bağın hedefinde koşardı)
    dosya_hedef = os.path.join(kok, "w12b", "hedef")
    _yaz(dosya_hedef, b"x\n")
    s.kirmizi_olmali("(12) dlg hedefi bir dosya", lambda: kaynak_cek(dlg_kilit_k, dosya_hedef, True), "hedef dizin boş değil")
    if os.name == "posix":
        bos_dizin = os.path.join(kok, "w12c", "bos")
        os.makedirs(bos_dizin)
        bag_hedef = os.path.join(kok, "w12c", "hedef")
        os.symlink(bos_dizin, bag_hedef)
        s.kirmizi_olmali("(12) dlg hedefi boş dizine sembolik bağ", lambda: kaynak_cek(dlg_kilit_k, bag_hedef, True), "hedef dizin boş değil")
        s.tamam("(12) sembolik bağın hedefinde git init koşmadı", os.listdir(bos_dizin) == [])
    # (11) lock'ta dlg olmadan fetch: freetype'ın gitlink'i serbest değil (kilit_dogrula çalışma ağacından önce commit ağacına bakar)
    bosluk_kilit = dict(sahte, sources=[x for x in sahte["sources"] if x["name"] != "dlg"])
    s.kirmizi_olmali("(11) lock'ta dlg yok: İ1 gitlink'i serbest değil (sonra)", lambda: _sonra_dogrula(w10, bosluk_kilit), "gitlink (alt modül) var")

    # --- ağaç içi indirme denetimi (M2): kilitli ağaçların içine derleme sırasında inen kaynaklar ---
    w7 = os.path.join(kok, "w7")
    fetch(w7, sahte, True)
    sahte_sp = _sahte_kilit(os.path.join(kok, "k6"), {"harfbuzz": {"subprojects/zlib.wrap": (b"[wrap-file]\n", "100644"),
                                                                  "subprojects/kayitli/x": (b"x", "100644")}})
    w8 = os.path.join(kok, "w8")
    fetch(w8, sahte_sp, True)
    hb_k = [x for x in sahte_sp["sources"] if x["name"] == "harfbuzz"][0]
    s.tamam("ağaç içi: temiz ağaçlarda boş liste",
            agac_ici_sorunlari(os.path.join(w8, "src", "harfbuzz"), hb_k["commit"], "harfbuzz") == [] and
            agac_ici_sorunlari(w7, sahte["sources"][0]["commit"], NEXT_ADI) == [])

    def temiz_mi(w, kilit):
        try:
            _sonra_dogrula(w, kilit)
            return True
        except Kirmizi:
            return False

    def ic_dene(ad, parca, w, kilit, kaldir, girdiler):
        """girdiler: [(göreli yol, bayt)] yazılır, iki aşamada da kırmızı beklenir; sonra kaldir (göreli üst yol) silinir
        ve ağacın yeniden temiz olduğu doğrulanır (temizlik yanlış kırmızıyı gizlemesin)."""
        for goreli, veri in girdiler:
            _yaz(os.path.join(w, *goreli.split("/")), veri)
        for asama in ("sonra", "once"):
            s.kirmizi_olmali("ağaç içi (%s): %s" % (asama, ad), lambda: kilit_dogrula(w, kilit, asama, True), parca)
        tam = os.path.join(w, *kaldir.split("/"))
        if os.path.isdir(tam):
            _sil(tam)
        else:
            os.remove(tam)
        s.tamam("ağaç içi: %s geri alınınca ağaç yeniden temiz" % ad, temiz_mi(w, kilit))

    ic_dene("iç içe .git dizini", ".git var", w7, sahte, "src/ffmpeg/alt", [("src/ffmpeg/alt/.git/HEAD", b"ref: x\n")])
    ic_dene("iç içe .GIT dizini (büyük harf; APFS büyük-küçük harf duyarsız)", ".git var", w7, sahte, "src/ffmpeg/alt3", [("src/ffmpeg/alt3/.GIT/HEAD", b"ref: x\n")])
    ic_dene("iç içe .Git dosyası (karışık harf)", ".git var", w7, sahte, "src/ffmpeg/alt4", [("src/ffmpeg/alt4/.Git", b"gitdir: x\n")])
    ic_dene("iç içe .git dosyası (alt modül göstergesi)", ".git var", w7, sahte, "src/ffmpeg/alt2",
            [("src/ffmpeg/alt2/.git", b"gitdir: ../../.git/modules/alt2\n")])
    ic_dene("(15) freetype .git/modules/dlg", ".git/modules", w7, sahte, "src/freetype/.git/modules", [("src/freetype/.git/modules/dlg/HEAD", b"x\n")])
    ic_dene("dlg'nin git dizininde modules/", ".git/modules", w7, sahte, "src/freetype/subprojects/dlg/.git/modules",
            [("src/freetype/subprojects/dlg/.git/modules/x/HEAD", b"x\n")])
    ic_dene("dlg içinde iç içe .git", ".git var", w7, sahte, "src/freetype/subprojects/dlg/ara", [("src/freetype/subprojects/dlg/ara/.git/HEAD", b"ref: x\n")])
    ic_dene("iç içe kaynak yoluna benzeyen subprojects/dlgx/ (önek eşleşmesi yok)", "dizin değil", w7, sahte, "src/freetype/subprojects/dlgx",
            [("src/freetype/subprojects/dlgx/.git/HEAD", b"ref: x\n")])
    ic_dene("(16) harfbuzz subprojects/zlib-1.3.1/.git", "dizin değil", w7, sahte, "src/harfbuzz/subprojects",
            [("src/harfbuzz/subprojects/zlib-1.3.1/.git/HEAD", b"ref: x\n")])
    ic_dene("Next kök ağacında iç içe .git", ".git var", w7, sahte, "scripts", [("scripts/.git", b"gitdir: x\n")])
    ic_dene("ağaçta olmayan subprojects/<x>/ dizini", "dizin değil", w7, sahte, "src/harfbuzz/subprojects",
            [("src/harfbuzz/subprojects/zlib-1.3/meson.build", b"x\n")])
    ic_dene("iç içe ağaçta subprojects/<x>/", "dizin değil", w7, sahte, "src/libass/ara", [("src/libass/ara/subprojects/y/z", b"x\n")])
    ic_dene("Next kökünde subprojects/<x>/", "dizin değil", w7, sahte, "subprojects", [("subprojects/x/y", b"x\n")])
    ic_dene("kilitli subprojects/ içinde packagecache", "dizin değil", w8, sahte_sp, "src/harfbuzz/subprojects/packagecache",
            [("src/harfbuzz/subprojects/packagecache/a.tar.gz", b"x\n")])
    ic_dene("kilitli subprojects/ içinde ağaçta olmayan alt dizin", "dizin değil", w8, sahte_sp, "src/harfbuzz/subprojects/zlib-1.3",
            [("src/harfbuzz/subprojects/zlib-1.3/x", b"x\n")])
    # yanlış alarm yok: ağaçta dizin olarak bulunan subprojects/kayitli/ içindeki ve Next'in prebuilt/ çıktısındaki girişler
    _yaz(os.path.join(w8, "src", "harfbuzz", "subprojects", "kayitli", "uretilen.o"), b"x")
    _yaz(os.path.join(w8, "prebuilt", "apple", "subprojects", "x", ".git"), b"derleme ciktisi")
    s.tamam("ağaç içi: kilitli dizinin içindeki yeni dosya ve Next prebuilt/ çıktısı yanlış alarm vermez", temiz_mi(w8, sahte_sp))
    # (5) gitlink SHA'sı kilitteki dlg commit'inden farklı: gitlink yolu "dizin" sayılmaz, İ1 de sağlanmaz (checkout boş dizin açar)
    gl = _sahte_kilit(os.path.join(kok, "k7"), {"freetype": {"subprojects/dlg": ("a" * 40, "160000")}})
    w9 = os.path.join(kok, "w9")
    fetch(w9, gl, True)
    s.kirmizi_olmali("(5) gitlink SHA'sı kilitteki dlg commit'inden farklı", lambda: _sonra_dogrula(w9, gl), "gitlink (alt modül) var")
    s.kirmizi_olmali("(14) arşiv: İ1'e uymayan gitlink", lambda: arsiv_yaz(w9, gl, "deneme-abc1234", os.path.join(kok, "arsiv-gl"), [("build.py", os.path.join(HERE, "build.py"))]),
                     "blob olmayan girdi")

    # --- kaynak arşivi ---
    work1 = os.path.join(kok, "w6")
    fetch(work1, sahte, True)     # arşiv ve paket sınamaları için kirletilmemiş çalışma dizini
    cikti = os.path.join(kok, "arsiv")
    v15 = [("build.py", os.path.join(HERE, "build.py"))]
    yol_ars = arsiv_yaz(work1, sahte, "deneme-abc1234", cikti, v15)
    with tarfile.open(yol_ars, "r:xz") as tar:
        adlar = set(tar.getnames())
        uyeler = dict((m.name, m) for m in tar.getmembers())
        s.tamam("arşiv: export-ignore'lu dosya da arşivde (git archive olmadan)", "ffmpeg/README" in adlar)
        s.tamam("arşiv: 15 ağaç (dlg freetype içinde) + SOURCES.json + V15 dosyası",
                all(arsiv_oneki(a) + "/README" in adlar for a in KAYNAK_ADLARI) and "SOURCES.json" in adlar and "forge-ffmpeg/build.py" in adlar)
        s.tamam("arşiv: dlg ağacı freetype/subprojects/dlg altında (başlık ve lisans dosyası dahil), üst düzeyde dlg/ yok",
                "freetype/subprojects/dlg/include/dlg/dlg.h" in adlar and "freetype/subprojects/dlg/LICENSE" in adlar and not any(n.startswith("dlg/") for n in adlar))
        s.tamam("arşiv: İ1 gitlink'i dosya olarak yazılmaz; FreeType ve opus .gitmodules normal dosya",
                "freetype/subprojects/dlg" not in adlar and tar.extractfile("freetype/.gitmodules").read() == FREETYPE_GITMODULES_ICERIK
                and tar.extractfile("opus/.gitmodules").read() == b"")
        s.tamam("arşiv: çalıştırılabilir bit ve bağ korunur", uyeler["ffmpeg/bin/arac"].mode == 0o755 and uyeler["ffmpeg/baglanti"].issym() and uyeler["ffmpeg/baglanti"].linkname == "README")
        s.tamam("arşiv: dosya baytları commit nesnesiyle aynı", tar.extractfile("ffmpeg/README").read() == b"kaynak ffmpeg\n")
        s.tamam("arşiv: gas-preprocessor ağacı kilitli baytlarıyla (dönüşümsüz) ve çalıştırılabilir bitiyle arşivde",
                tar.extractfile("gas-preprocessor/gas-preprocessor.pl").read() == GAS_SAHTE and uyeler["gas-preprocessor/gas-preprocessor.pl"].mode == 0o755)
        kayit = json.loads(tar.extractfile("SOURCES.json").read())
        s.tamam("arşiv: SOURCES.json ağaç SHA'sı dolu", all(COMMIT_RE.fullmatch(k["tree"]) for k in kayit) and len(kayit) == 15)
        dlg_kayit = [k for k in kayit if k["name"] == "dlg"]
        s.tamam("arşiv: SOURCES.json dlg kaydı parent ve path taşır, diğerleri taşımaz",
                len(dlg_kayit) == 1 and dlg_kayit[0]["parent"] == "freetype" and dlg_kayit[0]["path"] == "subprojects/dlg"
                and all("parent" not in k and "path" not in k for k in kayit if k["name"] != "dlg"))
        # Ağaç üyeleri: her kaynağın commit'indeki blob'lar kendi önekiyle; İ1 gitlink'i dosya olarak yoktur, yerine dlg ağacı vardır.
        beklenen_uyeler = set()
        for k in sahte["sources"]:
            for satir in git_ok(["ls-tree", "-r", k["commit"]], agac_yolu(work1, k["name"])).decode().splitlines():
                bilgi, _, n = satir.partition("\t")
                if bilgi.split()[0] != "160000":
                    beklenen_uyeler.add(arsiv_oneki(k["name"]) + "/" + n)
        s.tamam("arşiv: ağaç üyeleri commit'lerle birebir aynı (öznitelikle elenen dosya yok, gitlink yerinde dlg ağacı)",
                set(n for n in adlar if n != "SOURCES.json" and not n.startswith("forge-ffmpeg/")) == beklenen_uyeler)
    s.kirmizi_olmali("arşiv: geçersiz etiket", lambda: arsiv_yaz(work1, sahte, "v1", os.path.join(kok, "arsiv2"), v15), "etiket")
    ikili_kaynak = dict(sahte, sources=sahte["sources"] + [sahte["sources"][1]])
    s.kirmizi_olmali("arşiv: aynı üye iki kez yazılırsa (yinelenen ad)", lambda: arsiv_yaz(work1, ikili_kaynak, "deneme-abc1234", os.path.join(kok, "arsiv-cift"), v15), "yinelenen ad")
    s.kirmizi_olmali("arşiv: aynı çıktı yeniden", lambda: arsiv_yaz(work1, sahte, "deneme-abc1234", cikti, v15), "zaten var")

    # --- yamalar ---
    yk = os.path.join(kok, "y1")
    gw = _yama_agaclari(yk)
    gercek_yamalar = os.path.join(HERE, "patches")
    # gerçek yamalar, Next'ten alınmış çapa bölgelerine uygulanır (ffmpeg.sh yaması ağaç adı Next'tir: work kökü)
    rapor = yama_uygula(gw, gercek_yamalar)
    s.tamam("gerçek yamalar uygulanır (dokuz yama tanımı)", len(rapor) == 9)
    with open(os.path.join(gw, "scripts", "apple", "ffmpeg.sh"), "rb") as f:
        sonra = f.read().decode()
    # configure çağrısının belirteçleri: bayraklar POSTFIX'ten önce, bu sırayla
    cagri = [t for t in sonra.replace("\\\n", " ").split() if t.startswith("--") or t.startswith("${")]
    gunluk = cagri[cagri.index("--disable-network"):cagri.index("${CONFIGURE_POSTFIX}")]
    s.tamam("ffmpeg.sh yaması: beş bayrak POSTFIX'ten önce ve bu sırada", gunluk == list(GUVENLIK_BAYRAKLARI))
    s.tamam("ffmpeg.sh yaması: güvenlik kuralı boş liste verir",
            guvenlik_bayrak_sorunlari([t for t in cagri if t != "${CONFIGURE_POSTFIX}"] + ["--enable-libvpx", "--enable-libass"]) == [])
    with open(os.path.join(gw, "scripts", "source.sh"), "rb") as f:
        s.tamam("source.sh yaması: expat R_2_9_0", b'SOURCE_ID="R_2_9_0"' in f.read())
    with open(os.path.join(gw, "scripts", "apple", "harfbuzz.sh"), "rb") as f:
        hb = f.read().decode()
    s.tamam("harfbuzz.sh yaması: --wrap-mode=nodownload tam bir kez ve --buildtype=release'ten hemen önce",
            hb.count("--wrap-mode=nodownload") == 1 and "  --wrap-mode=nodownload \\\n  --buildtype=release \\\n" in hb)
    s.tamam("harfbuzz.sh yaması: meson setup satırından sonra gelir (seçenek komutun içinde)",
            hb.index('setup "${BUILD_DIR}"') < hb.index("--wrap-mode=nodownload") < hb.index("--default-library"))
    with open(os.path.join(gw, "scripts", "apple", "freetype.sh"), "rb") as f:
        ft = f.read().decode()
    s.tamam("freetype.sh yaması (brotli): --with-brotli=no tam bir kez, yes yok, komşu seçenekler yerinde",
            ft.count("--with-brotli=no \\\n") == 1 and "brotli=yes" not in ft and "  --with-png \\\n  --with-brotli=no \\\n  --with-sysroot=" in ft)
    with open(os.path.join(gw, "src", "freetype", "autogen.sh"), "rb") as f:
        ag = f.read().decode()
    s.tamam("autogen.sh yaması: git submodule update --init kalkar, yerine kapalı başarısız exit 1 gelir",
            "git submodule update" not in ag and ag.count("    exit 1\n") == 1 and 'echo "forge-ffmpeg: subprojects/dlg kilitten gelmeli; ağdan alınmaz" >&2\n    exit 1\n  fi\n' in ag)
    with open(os.path.join(gw, "scripts", "function-apple.sh"), "rb") as f:
        fa = f.read().decode()
    s.tamam("function-apple.sh yaması (freetype2.pc): blokta brotli 0 kez, Requires.private: zlib 1 kez, komşu satırlar yerinde",
            "brotli" not in fa.lower() and fa.count("Requires.private: zlib\n") == 1 and fa.count("Requires.private:") == 1
            and "Requires: libpng\nRequires.private: zlib\nLibs: -L\\${libdir} -lfreetype\n" in fa)
    s.tamam("function-apple.sh yaması: yalnız o satır değişir (blok geri kalanı aynı)",
            fa == NEXT_FUNCTION_APPLE_SH.replace("Requires.private: zlib, libbrotlidec\n", "Requires.private: zlib\n"))
    with open(os.path.join(gw, "scripts", "main-ios.sh"), "rb") as f:
        mi = f.read().decode()
    eski_satir = "      if [[ $OK_libpng -eq 1 ]] && [[ $OK_libjxl -eq 1 ]]; then\n"
    yeni_satir = "      if [[ $OK_libpng -eq 1 ]]; then\n"
    s.tamam("main-ios.sh yaması (SK-941-2): OK_libjxl 0 kez, yeni satır 1 kez, eski satır yok",
            mi.count("OK_libjxl") == 0 and mi.count(yeni_satir) == 1 and eski_satir not in mi)
    s.tamam("main-ios.sh yaması: yalnız freetype koşulu değişir (geri kalanı aynı)", mi == NEXT_MAIN_IOS_SH.replace(eski_satir, yeni_satir))
    kol_ = lambda metin, ad: metin[metin.index("    %s)\n" % ad):].split("      ;;\n")[0]
    s.tamam("main-ios.sh yaması: fontconfig ve gnutls kolları bayt bayt aynı, fontconfig ve harfbuzz hâlâ OK_freetype bekler",
            kol_(mi, "fontconfig") == kol_(NEXT_MAIN_IOS_SH, "fontconfig") and kol_(mi, "gnutls") == kol_(NEXT_MAIN_IOS_SH, "gnutls")
            and "[[ $OK_freetype -eq 1 ]]" in kol_(mi, "fontconfig") and "[[ $OK_freetype -eq 1 ]]" in kol_(mi, "harfbuzz"))
    # Yama 08/09 (SK-947-5): Next'in gas-preprocessor indirme dalı kapanır, araç zinciri hatası derlemeyi durdurur.
    with open(os.path.join(gw, "scripts", "function-ios.sh"), "rb") as f:
        fi_ = f.read().decode()
    with open(os.path.join(gw, "scripts", "run-apple.sh"), "rb") as f:
        ra_ = f.read().decode()
    dal_basi = '  if [ ! -f "${FFMPEG_KIT_TMPDIR}/gas-preprocessor.pl" ]; then\n'
    dal_sonu = '  fi\n\n  LOCAL_GAS_PREPROCESSOR="${FFMPEG_KIT_TMPDIR}/gas-preprocessor.pl"\n'
    iz_satiri = 'echo -e "\\nERROR: gas-preprocessor.pl was not placed by build.py before ios.sh; the build must not fetch it. Build will now exit.\\n" 1>>"${BASEDIR}"/build.log 2>&1'
    s.tamam("function-ios.sh yaması: indirme, curl, chmod ve sed çağrıları kalkar; dalda log satırı ve tek koşulsuz exit 1 var",
            "download" not in fi_ and "curl" not in fi_ and "chmod" not in fi_ and "SED_INLINE" not in fi_ and "return 1" not in fi_
            and fi_.count("exit 1") == 1 and fi_.count(iz_satiri) == 1 and fi_.count("DOWNLOAD_RESULT") == 0)
    s.tamam("function-ios.sh yaması: dal koşulu ve sonrası yerinde, dal içi yalnız log satırı ve exit 1",
            fi_.startswith(NEXT_FUNCTION_IOS_SH[:NEXT_FUNCTION_IOS_SH.index(dal_basi)])
            and fi_[fi_.index(dal_basi):] == dal_basi + "    " + iz_satiri + "\n    exit 1\n" + dal_sonu)
    s.tamam("function-ios.sh yaması: bekçinin aradığı iki indirme satırını (DEBUG: Downloaded / ERROR: Failed to download) üretmez",
            "DEBUG: Downloaded " not in fi_ and "ERROR: Failed to download" not in fi_ and "not found" not in fi_ and "libbrotlidec" not in fi_)
    s.tamam("run-apple.sh yaması: set_toolchain_paths çağrısı || exit 1 alır, geri kalanı bayt bayt aynı",
            ra_ == NEXT_RUN_APPLE_SH.replace('set_toolchain_paths "${LIB_NAME}"\n', 'set_toolchain_paths "${LIB_NAME}" || exit 1\n') and ra_.count("|| exit 1") == 1)
    if os.name == "posix" and shutil.which("bash"):
        # Gerçek bash'te: yamalı dal dosya yokken betiği durdurur ve indirmez; dosya varken geçer. Yamasız hâl hatayı yutar (download()
        # içindeki exit 1 komut ikamesinde kalır, chmod başarısız olur, return 1 ile devam edilir): sınama yamayı gerçekten ayırt eder.
        bh = os.path.join(kok, "bash-gas")
        os.makedirs(os.path.join(bh, ".tmp"))

        def gas_bash(fonksiyon, hazir):
            yer = os.path.join(bh, ".tmp", GAS_DOSYASI)
            for yol_ in (yer, os.path.join(bh, "build.log"), os.path.join(bh, "indirme.iz")):
                if os.path.lexists(yol_):
                    os.remove(yol_)
            if hazir:
                _yaz(yer, b"#!/usr/bin/env perl\n")
            _yaz(os.path.join(bh, "build.log"), b"")
            kesik = fonksiyon.split("LOCAL_GAS_PREPROCESSOR=")[0] + "  LOCAL_GAS_PREPROCESSOR=x\n}\n"
            betik = ('BASEDIR="$1"\nFFMPEG_KIT_TMPDIR="$BASEDIR/.tmp"\nset_host_cc() { :; }\n'
                     'download() { echo indirme >> "$BASEDIR/indirme.iz"; exit 1; }\n' + kesik + "set_toolchain_paths x\necho DEVAM\n")
            p = subprocess.run(["bash", "-c", betik, "bash", bh], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            with open(os.path.join(bh, "build.log"), "rb") as f:
                return (p.returncode, b"DEVAM" in p.stdout, f.read().decode(), os.path.lexists(os.path.join(bh, "indirme.iz")))

        yamali_yok = gas_bash(fi_, False)
        s.tamam("function-ios.sh yaması (bash): dosya yokken exit 1, log satırı yazılır, indirme çağrılmaz, betik devam etmez",
                yamali_yok[0] == 1 and not yamali_yok[1] and "ERROR: gas-preprocessor.pl was not placed" in yamali_yok[2] and not yamali_yok[3])
        yamali_var = gas_bash(fi_, True)
        s.tamam("function-ios.sh yaması (bash): dosya yerindeyken geçer, log satırı yok, indirme çağrılmaz",
                yamali_var[0] == 0 and yamali_var[1] and "ERROR" not in yamali_var[2] and not yamali_var[3])
        yamasiz_yok = gas_bash(NEXT_FUNCTION_IOS_SH, False)
        s.tamam("function-ios.sh yamasız (bash): hatayı yutar ve devam eder (yamanın gerekçesi)", yamasiz_yok[1] and yamasiz_yok[3])

        def ra_kes(metin):
            bas, _, son = metin.partition("\nset_toolchain_paths")
            bas = "\n".join(l for l in bas.split("\n") if not l.startswith("source "))
            return bas + "\nset_toolchain_paths" + son.split("\n")[0] + "\necho DEVAM\n"

        for ad_, metin_, devam_ in (("yamalı", ra_, False), ("yamasız", NEXT_RUN_APPLE_SH, True)):
            p = subprocess.run(["bash", "-c", "set_toolchain_paths() { return 1; }\nLIB_NAME=x\n" + ra_kes(metin_)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            s.tamam("run-apple.sh %s (bash): set_toolchain_paths 1 dönerse betik %s" % (ad_, "devam eder" if devam_ else "exit 1 ile durur"),
                    (b"DEVAM" in p.stdout) == devam_ and (devam_ or p.returncode == 1))
    # Yama 07 libjxl'in kapalı olduğuna dayanır (SK-941-3): freetype açık, libjxl bayrakla kapalı, açan bayrak yok.
    s.tamam("ios.sh seçenekleri: freetype açık, --disable-lib-libjxl tam bir kez, libjxl'i açan bayrak yok",
            "--enable-lib-freetype" in IOS_SECENEKLERI and IOS_SECENEKLERI.count("--disable-lib-libjxl") == 1
            and not [o for o in IOS_SECENEKLERI if "libjxl" in o and o != "--disable-lib-libjxl"])
    # gerçek freetype yamaları çapa yokken ya da iki kez varken uygulanmaz (SK-936-3, SK-938-4, brotli)
    for ad_, dosya_, parca_, iki_ in (("autogen.sh", ("src", "freetype", "autogen.sh"), FREETYPE_AUTOGEN_SH, "    git submodule update --init\n"),
                                      ("freetype.sh", ("scripts", "apple", "freetype.sh"), NEXT_FREETYPE_SH, "  --with-brotli=yes \\\n"),
                                      ("function-apple.sh", ("scripts", "function-apple.sh"), NEXT_FUNCTION_APPLE_SH, "Requires.private: zlib, libbrotlidec\n"),
                                      ("main-ios.sh", ("scripts", "main-ios.sh"), NEXT_MAIN_IOS_SH, "      if [[ $OK_libpng -eq 1 ]] && [[ $OK_libjxl -eq 1 ]]; then\n"),
                                      ("function-ios.sh", ("scripts", "function-ios.sh"), NEXT_FUNCTION_IOS_SH,
                                       NEXT_FUNCTION_IOS_SH[NEXT_FUNCTION_IOS_SH.index("    DOWNLOAD_RESULT="):NEXT_FUNCTION_IOS_SH.index("  fi\n\n  LOCAL_GAS")]),
                                      ("run-apple.sh", ("scripts", "run-apple.sh"), NEXT_RUN_APPLE_SH, 'set_toolchain_paths "${LIB_NAME}"\n')):
        fw_ = _yama_agaclari(os.path.join(kok, "yf-" + ad_))
        _yaz(os.path.join(fw_, *dosya_), (parca_ + iki_).encode())
        s.kirmizi_olmali("%s yaması: çapa iki kez bulunursa uygulanmaz" % ad_, lambda: yama_uygula(fw_, gercek_yamalar), "2 kez")
        fw2_ = _yama_agaclari(os.path.join(kok, "yg-" + ad_))
        _yaz(os.path.join(fw2_, *dosya_), b"degisti\n")
        s.kirmizi_olmali("%s yaması: çapa yoksa (üst akış değişmişse) uygulanmaz" % ad_, lambda: yama_uygula(fw2_, gercek_yamalar), "0 kez")
    # gerçek harfbuzz yaması yanlış ağaçta ya da çapa iki kez varken kırmızı verir
    hw = _yama_agaclari(os.path.join(kok, "y3"))
    _yaz(os.path.join(hw, "scripts", "apple", "harfbuzz.sh"), (NEXT_HARFBUZZ_SH + "  --buildtype=release \\\n").encode())
    s.kirmizi_olmali("harfbuzz.sh yaması: çapa iki kez bulunursa uygulanmaz", lambda: yama_uygula(hw, gercek_yamalar), "2 kez")
    hw2 = _yama_agaclari(os.path.join(kok, "y4"))
    _yaz(os.path.join(hw2, "scripts", "apple", "harfbuzz.sh"), b'meson setup "${BUILD_DIR}" \\\n  --default-library=static \\\n')
    s.kirmizi_olmali("harfbuzz.sh yaması: çapa yoksa (Next betiği değişmişse) uygulanmaz", lambda: yama_uygula(hw2, gercek_yamalar), "0 kez")
    # olumsuz yamalar
    yd = os.path.join(kok, "yd")
    yw = _yama_agaclari(os.path.join(kok, "y2"))

    def yama_dene(ad, parca, **ust):
        if os.path.isdir(yd):
            shutil.rmtree(yd)
        _yama_json(os.path.join(yd, "t1.json"), **ust)
        s.kirmizi_olmali("yama: " + ad, lambda: yama_uygula(yw, yd), parca)

    yama_dene("çapa yok", "0 kez", anchor="ÇAPA_YOK")
    yama_dene("çapa iki kez", "2 kez", anchor="SOURCE_TYPE", text="SOURCE_TYPE")
    yama_dene("dosya değişmiyor", "değişmedi", text='SOURCE_ID="R_2_8_1"')
    yama_dene("dosya ağaç dışında", "dışında", file="../../kacak.sh")
    yama_dene("dosya yok", "dosya yok", file="scripts/yok.sh")
    yama_dene("bilinmeyen ağaç", "tree", tree="yok")
    yama_dene("bildirim eksik", "anahtarlar", notice=None)
    yama_dene("tarih biçimi", "date", date="8.11.2026")
    yama_dene("bilinmeyen işlem", "op", op="sil")
    s.kirmizi_olmali("yama: boş dizin", lambda: yamalari_oku(os.path.join(kok, "bos-yama")), "dizini yok")

    # --- libpng ölçümü ---
    lw = os.path.join(kok, "lp")
    s.kirmizi_olmali("libpng: pngpriv.h yoksa ölçülemez", lambda: libpng_olcumu(lw), "pngpriv.h yok")
    temiz_png = b"#ifndef PNGPRIV_H\n#if defined(__APPLE__)\n#endif\n#if TARGET_OS_MACCATALYST\n#endif\n"
    _yaz(os.path.join(lw, "src", "libpng", "pngpriv.h"), temiz_png)
    olcum = libpng_olcumu(lw)
    s.tamam("libpng: koşul yoksa ölçüm satırı özetle birlikte döner", "TARGET_OS_MAC yok" in olcum and hashlib.sha256(temiz_png).hexdigest()[:12] in olcum)
    for ad, icerik in (("#if TARGET_OS_MAC", b"#if TARGET_OS_MAC\n#endif\n"), ("defined(TARGET_OS_MAC)", b"# if defined(TARGET_OS_MAC) && TARGET_OS_MAC\n"),
                       ("dosya sonunda", b"x\n#define A TARGET_OS_MAC")):
        _yaz(os.path.join(lw, "src", "libpng", "pngpriv.h"), icerik)
        s.kirmizi_olmali("libpng: pngpriv.h içinde " + ad, lambda: libpng_olcumu(lw), "TARGET_OS_MAC içeriyor")

    # --- build.yml: m1 (özet değişkene alınıp denetlenir) ve m2 (sürümler sabit değil, etiketten ve kilitten) ---
    yml_yol = os.path.join(HERE, ".github", "workflows", "build.yml")
    s.tamam("build.yml var", os.path.isfile(yml_yol))
    if os.path.isfile(yml_yol):
        with open(yml_yol, "rb") as f:
            yml = f.read().decode("utf-8")
        s.tamam("build.yml: özet echo içinde komut ikamesiyle yazılmaz", 'echo "sha256sums=$(' not in yml and "[0-9a-f]{64}" in yml)
        s.tamam("build.yml: release notunda sabit sürüm yok, kilitten türetilir",
                re.search(r"(FFmpegKitNext|FFmpeg) [vn]?[0-9]+\.[0-9]+", yml) is None and "dist/source-lock.json" in yml)
        if os.name == "posix" and shutil.which("bash") and shutil.which("python3"):
            _yml_adim_sinamalari(s, kok, yml)

    # --- check ---
    ff = _sahte_kume(os.path.join(kok, "c0"))
    xc0 = _sahte_xc(os.path.join(kok, "c0"))
    ot = _otool_sahte()
    satirlar, kume = check(xc0, ff, ot)
    s.tamam("check olumlu: iki dilim configure satırı ve küme", len(satirlar) == 2 and ("protocol", "file") in kume and ("demuxer", "mov") in kume and ("decoder", "h264") in kume)

    # --- configure dizesi: FFmpeg'in gerçek tırnaklama biçimi (SK-943-1) ---
    s.tamam("fixture: _ham(_configure()) elle yazılmış gerçek biçimin aynısı", _ham(_configure()) == GERCEK_TEMEL_HAM and "'hls,dash,concat,image2'" in GERCEK_TEMEL_HAM)
    s.tamam("check olumlu: satırlar ikilideki ham dizedir (belirteçlerin yeniden birleşimi değil)", satirlar == [(d, GERCEK_TEMEL_HAM) for d in DILIMLER])
    gdizeler, _ = yapilandirma_dizeleri(_sahte_ikili(GERCEK_HAM))
    gtok = gdizeler[0][1] if len(gdizeler) == 1 else []
    s.tamam("yapilandirma_dizeleri: ham dize ve tırnak kurallarıyla ayrılmış belirteçler (virgüllü, boşluklu, içinde tek tırnaklı değer)",
            len(gdizeler) == 1 and gdizeler[0][0] == GERCEK_HAM and "--disable-demuxer=hls,dash,concat,image2" in gtok
            and "--extra-cflags=-O2 -DX" in gtok and "--extra-cxxflags=-DNAME='x'" in gtok and "--disable-muxer=tee" in gtok and "--enable-zlib" in gtok)
    xcz = _sahte_xc(os.path.join(kok, "c0z"), GERCEK_HAM)
    zsatirlar, _ = check(xcz, _sahte_kume(os.path.join(kok, "c0z")), ot)
    s.tamam("check olumlu: elle yazılmış gerçek biçim (virgüllü, boşluklu, tek tırnaklı değerler) geçer, satır ham", zsatirlar == [(d, GERCEK_HAM) for d in DILIMLER])
    for ad_, girdi_, beklenen_ in (("değer tırnaklanır", "--a=b c", "--a='b c'"), ("boş değer tırnaksız", "--a=", "--a="), ("= yok, güvenli", "plain", "plain"),
                                   ("değerde tek tırnak", "--a=it's", "--a='it'\\''s'"), ("= yok, boşluklu", "a b", "'a b'"),
                                   ("ilk = ayırır, ikincisi değerde kalır", "--a=b=c", "--a='b=c'"), ("virgül", "--a=x,y", "--a='x,y'"),
                                   ("küme içi karakterler tırnaksız", "--a=A-z_0/9.+-", "--a=A-z_0/9.+-"), ("ad kısmı tırnaklanmaz", "--a b=c", "--a b=c")):
        s.tamam("belirtec_tirnakla: %s" % ad_, belirtec_tirnakla(girdi_) == beklenen_)
    s.tamam("belirtec_ayir: tırnaklı '#' değerde yorum sayılmaz", belirtec_ayir("--x='a #b' --y") == ["--x=a #b", "--y"])
    for ad_, ham_ in (("kapanmamış tek tırnak", "--a='b c --d"), ("sonda kaçış", "--a=b\\")):
        s.kirmizi_olmali("belirtec_ayir: %s" % ad_, lambda: belirtec_ayir(ham_), "ayrıştırılamadı")
    kirmizi_mesaj = ""
    try:
        belirtec_ayir("--a=b  --c=d")
    except Kirmizi as e:
        kirmizi_mesaj = str(e)
    s.tamam("belirtec_ayir: çift boşlukta ilk farkın konumu ve kesit mesajda", "ilk fark 6. bayt" in kirmizi_mesaj and "--c=d" in kirmizi_mesaj)
    uzun_mesaj = ""
    try:
        belirtec_ayir("--a=b  " + "y" * 200)
    except Kirmizi as e:
        uzun_mesaj = str(e)
    s.tamam("belirtec_ayir: mesajdaki kesit en çok 40 bayt", "ilk fark" in uzun_mesaj and 0 < len(uzun_mesaj.split("bayt: ", 1)[1].rstrip(")")) <= 40)
    gizli_mesaj = ""
    try:
        belirtec_ayir("--a=b  API_TOKEN=cok-gizli")
    except Kirmizi as e:
        gizli_mesaj = str(e)
    s.tamam("belirtec_ayir: mesajdaki kesit gizlenir", "ilk fark" in gizli_mesaj and "cok-gizli" not in gizli_mesaj and "API_TOKEN=***" in gizli_mesaj)

    sayac = [0]

    def check_dene(ad, parca, bayrak=None, lisans=b"LGPL version 3 or later", dilimler=DILIMLER, otool=None, kume_degistir=None, kume_atla=(), ekstra=None):
        sayac[0] += 1
        d = os.path.join(kok, "c-%d" % sayac[0])
        xc = _sahte_xc(d, bayrak, lisans, dilimler, ekstra)
        ffd = _sahte_kume(d, kume_degistir, kume_atla)
        s.kirmizi_olmali("check: " + ad, lambda: check(xc, ffd, otool or ot), parca)

    for b in GUVENLIK_BAYRAKLARI:
        check_dene("bayrak eksik " + b, b, _configure(cikar=(b,)))
    check_dene("enable-protocol=file,data", "enable-protocol", _configure(cikar=("--enable-protocol=file",), ekstra=("--enable-protocol=file,data",)))
    check_dene("ikinci enable-protocol", "enable-protocol", _configure(ekstra=("--enable-protocol=pipe",)))
    check_dene("enable-protocols", "enable-protocols", _configure(ekstra=("--enable-protocols",)))
    check_dene("enable-demuxer=hls", "--enable-demuxer", _configure(ekstra=("--enable-demuxer=hls",)))
    check_dene("enable-muxers", "--enable-muxer", _configure(ekstra=("--enable-muxers",)))
    check_dene("enable-network", "--enable-network", _configure(ekstra=("--enable-network",)))
    check_dene("sıra: disable-protocols sonra", "önce gelmiyor", ["--enable-protocol=file"] + TEMEL_CONFIGURE + ["--disable-network", "--disable-protocols", "--disable-demuxer=hls,dash,concat,image2", "--disable-muxer=tee"])
    check_dene("enable-gpl", "--enable-gpl", _configure(ekstra=("--enable-gpl",)))
    check_dene("enable-libx264", "--enable-libx264", _configure(ekstra=("--enable-libx264",)))
    check_dene("Next'in freetype ile açtığı libjxl", "libjxl", _configure(ekstra=("--enable-libjxl",)))
    check_dene("beklenmeyen harici kütüphane (libwebp)", "libwebp", _configure(ekstra=("--enable-libwebp",)))
    for lib in HARICI_KITAPLIKLAR:
        check_dene("harici kütüphane eksik " + lib, lib, [t for t in _configure() if t != lib])
    check_dene("version3 yok", "--enable-version3", [t for t in _configure() if t != "--enable-version3"])
    dem = "'hls,dash,concat,image2'"
    check_dene("demuxer değeri eksik (hls,dash,concat)", "--disable-demuxer=hls,dash,concat,image2 yok", GERCEK_TEMEL_HAM.replace(dem, "'hls,dash,concat'"))
    check_dene("demuxer değeri fazla (…,image2,mov)", "--disable-demuxer=hls,dash,concat,image2 yok", GERCEK_TEMEL_HAM.replace(dem, "'hls,dash,concat,image2,mov'"))
    check_dene("kapanmamış tırnak", "ayrıştırılamadı", GERCEK_TEMEL_HAM.replace(dem, "'hls,dash,concat,image2"))
    check_dene("bütün belirteç tırnaklı ('--disable-network')", "tırnaklama biçiminde değil", GERCEK_TEMEL_HAM.replace("--disable-network ", "'--disable-network' "))
    check_dene("çift tırnaklı değer", "tırnaklama biçiminde değil", GERCEK_TEMEL_HAM.replace(dem, '"hls,dash,concat,image2"'))
    check_dene("tırnaksız demuxer değeri (eski beklenti biçimi) kabul edilmez", "tırnaklama biçiminde değil", GERCEK_TEMEL_HAM.replace(dem, "hls,dash,concat,image2"))
    check_dene("belirteçler arasında çift boşluk", "tırnaklama biçiminde değil", GERCEK_TEMEL_HAM.replace("--disable-protocols --enable", "--disable-protocols  --enable"))
    check_dene("dizenin başında boşluk", "tırnaklama biçiminde değil", " " + GERCEK_TEMEL_HAM)
    check_dene("dizenin sonunda boşluk", "tırnaklama biçiminde değil", GERCEK_TEMEL_HAM + " ")
    check_dene("değerin içindeki tek tırnak çift tırnakla sarılı", "tırnaklama biçiminde değil", GERCEK_HAM.replace("'-DNAME='\\''x'\\'''", "\"-DNAME='x'\""))
    check_dene("enable-protocol='file,data' (tırnaklı, ikinci)", "enable-protocol", GERCEK_TEMEL_HAM + " --enable-protocol='file,data'")
    check_dene("GPL lisans dizesi", "LGPL", lisans=b"GPL version 3 or later")
    check_dene("configure dizesi yok", "bulunamadı", ["yok"])
    check_dene("fazla dilim", "dilim", dilimler=DILIMLER + ("ios-x86_64-simulator",))
    check_dene("eksik dilim", "dilim", dilimler=DILIMLER[:1])
    check_dene("otool: ana makine yolu", "izinli olmayan", otool=_otool_sahte(dep_l=["/opt/homebrew/lib/libfoo.dylib"]))
    check_dene("otool: mutlak rpath", "LC_RPATH", otool=_otool_sahte(rpath="/Users/runner/x"))
    check_dene("otool: kimlik @rpath değil", "LC_ID_DYLIB", otool=_otool_sahte(kimlik="/usr/local/lib/x.dylib"))
    check_dene("küme: ek protokol", "protokol", kume_degistir={"libavformat/protocol_list.c": ["file_protocol", "pipe_protocol"]})
    check_dene("küme: ağ protokolü", "protokol", kume_degistir={"libavformat/protocol_list.c": ["file_protocol", "http_protocol"]})
    for d in YASAK_DEMUXER:
        check_dene("küme: %s demuxer" % d, d, kume_degistir={"libavformat/demuxer_list.c": ["mov_demuxer", "%s_demuxer" % d]})
    check_dene("küme: tee muxer", "tee", kume_degistir={"libavformat/muxer_list.c": ["mp4_muxer", "tee_muxer"]})
    check_dene("küme: tanınmayan soneki", "soneki", kume_degistir={"libavformat/muxer_list.c": ["mp4_foo"]})
    check_dene("küme: liste dosyası yok", "yok", kume_atla=("libavcodec/bsf_list.c",))

    # --- derlenmiş listelerin üretecin dilbilgisiyle sıkı ayrıştırılması (SK-943-2, SK-943-3) ---
    s.tamam("KUME_DOSYALARI: dokuz liste, yapı ve dizi adları configure@946fcce'den elle yazılmış tabloyla aynı",
            sorted((d, y, a) for _, d, _, y, a in KUME_DOSYALARI) == sorted((d, y, a) for d, (y, a) in KUME_GERCEK.items()) and len(KUME_DOSYALARI) == 9
            and KUME_DOSYALARI[-1][0] == "outdev" and BOS_BEKLENEN_TUR == "outdev")
    gercek_outdev = b"static const FFOutputFormat * const outdev_list[] = {\n    NULL };\n"
    s.tamam("kume_dosyasi_ayristir: elle yazılmış gerçek boş outdev dosyası boş liste verir",
            kume_dosyasi_ayristir(gercek_outdev, "FFOutputFormat", "outdev_list", "o") == [] and _liste_dosyasi("libavdevice/outdev_list.c", []).encode() == gercek_outdev)
    s.tamam("kume_dosyasi_ayristir: girdiler sırayla",
            kume_dosyasi_ayristir(b"static const FFCodecParser * const parser_list[] = {\n    &ff_aac_parser,\n    &ff_h264_parser,\n    NULL };\n", "FFCodecParser", "parser_list", "p") == ["aac_parser", "h264_parser"])
    kume_ok = derlenmis_kume(ff)
    s.tamam("check olumlu: elle yazılmış gerçek biçim: protokol {file}, outdev girdisi yok, indev avfoundation ve lavfi, filter_list'in koşulsuz girdileri",
            set(n for t, n in kume_ok if t == "protocol") == set(["file"]) and not [1 for t, _ in kume_ok if t == "outdev"]
            and set(n for t, n in kume_ok if t == "indev") == set(["lavfi", "avfoundation"]) and ("filter", "vsink_buffer") in kume_ok)
    check_dene("küme: outdev'de audiotoolbox muxer'ı", "outdev kümesi boş değil", kume_degistir={"libavdevice/outdev_list.c": ["audiotoolbox_muxer"]})
    check_dene("küme: outdev'de tek bilinmeyen girdi", "outdev kümesi boş değil", kume_degistir={"libavdevice/outdev_list.c": ["x_muxer"]})
    check_dene("küme: outdev dosyası yok", "yok", kume_atla=("libavdevice/outdev_list.c",))
    for dosya_ in sorted(KUME_ORNEK):
        if dosya_ != "libavdevice/outdev_list.c":
            check_dene("küme: boş liste (%s)" % dosya_, "küme listesi boş", kume_degistir={dosya_: []})
        icerik_ = _liste_dosyasi(dosya_, KUME_ORNEK[dosya_])
        check_dene("küme: yanlış yapı adı (%s)" % dosya_, "başlık", kume_degistir={dosya_: _liste_dosyasi(dosya_, KUME_ORNEK[dosya_], yapi="AVCodecParser")})
        check_dene("küme: CRLF (%s)" % dosya_, "başlık", kume_degistir={dosya_: icerik_.replace("\n", "\r\n")})
        check_dene("küme: NULL sonlandırıcısı yok (%s)" % dosya_, "başlık" if KUME_ORNEK[dosya_] == [] else "sonlandırıcısı", kume_degistir={dosya_: icerik_[:-len("    NULL };\n")]})
        check_dene("küme: son satır sonu yok (%s)" % dosya_, "başlık" if KUME_ORNEK[dosya_] == [] else "sonlandırıcısı", kume_degistir={dosya_: icerik_[:-1]})
        check_dene("küme: fazladan satır (%s)" % dosya_, "beklenmeyen satır", kume_degistir={dosya_: icerik_[:-len("    NULL };\n")] + "    // not\n    NULL };\n"})
        check_dene("küme: ikinci NULL (%s)" % dosya_, "beklenmeyen satır", kume_degistir={dosya_: icerik_ + "    NULL };\n"})
        check_dene("küme: sonlandırıcıdan sonra çöp (%s)" % dosya_, "sonlandırıcısı", kume_degistir={dosya_: icerik_ + "x\n"})
        check_dene("küme: sonlandırıcı yerine 12 bayt çöp (%s)" % dosya_, "sonlandırıcısı",
                   kume_degistir={dosya_: icerik_[:-len("    NULL };\n")] + "    NULL };\r"})
        check_dene("küme: boş dosya (%s)" % dosya_, "başlık", kume_degistir={dosya_: ""})
        check_dene("küme: yalnız başlık (%s)" % dosya_, "başlık", kume_degistir={dosya_: icerik_.split("\n")[0] + "\n"})
    check_dene("küme: girintisiz girdi", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "&ff_mp4_muxer,\n    NULL")})
    check_dene("küme: girdi satırında CR", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "    &ff_mp4_muxer,\r\n    NULL")})
    check_dene("küme: virgülsüz girdi", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "    &ff_mp4_muxer\n    NULL")})
    check_dene("küme: virgülden önce boşluk", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "    &ff_mp4_muxer ,\n    NULL")})
    check_dene("küme: sekme girintisi", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "\t&ff_mp4_muxer,\n    NULL")})
    check_dene("küme: aynı satırda iki girdi", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "    &ff_mp4_muxer, &ff_null_muxer,\n    NULL")})
    check_dene("küme: yinelenen girdi", "yinelenen", kume_degistir={"libavformat/demuxer_list.c": ["mov_demuxer", "mp3_demuxer", "mov_demuxer"]})
    check_dene("küme: boş satır", "beklenmeyen satır", kume_degistir={"libavformat/muxer_list.c": _liste_dosyasi("libavformat/muxer_list.c", []).replace("    NULL", "\n    NULL")})
    check_dene("küme: outdev'de yalnız iki NULL", "beklenmeyen satır", kume_degistir={"libavdevice/outdev_list.c": _liste_dosyasi("libavdevice/outdev_list.c", []) + "    NULL };\n"})

    # --- brotli yok (patches/05): otool -L ve ikili baytları ---
    brotli_ikili = {"libavcodec.xcframework/%s/libavcodec.framework/libavcodec" % DILIMLER[0]: b"\xcf\xfa\xed\xfe_BrotliDecoderDecompress\x00"}
    check_dene("brotli sembolü ikilide", "brotli sembolü", ekstra=brotli_ikili)
    check_dene("brotli küçük harfli dize ikilide", "brotli sembolü", ekstra={k: b"\xcf\xfa\xed\xfelibbrotlidec\x00" for k in brotli_ikili})
    check_dene("otool -L brotli bağımlılığı (@rpath altında, yol izni geçse de)", "brotli bağımlılığı",
               otool=_otool_sahte(dep_l=["@rpath/libbrotlidec.framework/libbrotlidec"]))
    s.tamam("brotli_sorunlari: temiz çıktı ve temiz ikili boş liste", brotli_sorunlari("x:\n\t/usr/lib/libz.1.dylib (compatibility version 1.0.0)\n", b"\xcf\xfa\xed\xfeLGPL") == [])

    # --- FT_DEBUG_LOGGING ölçümü (SK-936-6) ve derleme ortamı izin listesi ---
    ftw = os.path.join(kok, "ftw")
    ftoption = os.path.join(agac_yolu(ftw, "freetype"), "include", "freetype", "config", "ftoption.h")
    gercek_satir = b"/* #define FT_DEBUG_LOGGING */\n#define FT_CONFIG_OPTION_SUBPIXEL_RENDERING\n"
    _yaz(ftoption, gercek_satir)
    _yaz(os.path.join(ftw, "scripts", "apple", "freetype.sh"), b"./configure --with-brotli=no\n")
    ol = ft_debug_olcumu(ftw, temiz_env(), [("ios-arm64", " ".join(_configure()))])
    s.tamam("FT_DEBUG_LOGGING ölçümü: yorumdaki tanım etkin değil; ölçüm satırı özet ve tarama kapsamını söyler",
            ol[0] == "freetype-olcum" and hashlib.sha256(gercek_satir).hexdigest()[:12] in ol[1] and "1 dosyada" in ol[1])
    for ad_, icerik in (("(19) etkin #define", b"#define FT_DEBUG_LOGGING\n"), ("(19) boşluklu #  define", b"  #  define FT_DEBUG_LOGGING 1\n"),
                        ("(19) tab ile", b"#\tdefine\tFT_DEBUG_LOGGING\n"), ("(19) dosya sonu satırsız", b"x\n#define FT_DEBUG_LOGGING")):
        _yaz(ftoption, icerik)
        s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: ftoption.h " + ad_, lambda: ft_debug_olcumu(ftw, temiz_env(), []), "etkin tanımlı")
    _yaz(ftoption, gercek_satir)
    s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: derleme ortamında", lambda: ft_debug_olcumu(ftw, dict(temiz_env(), CFLAGS="-DFT_DEBUG_LOGGING"), []), "derleme ortamı")
    s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: FFmpeg configure satırında", lambda: ft_debug_olcumu(ftw, temiz_env(), [("ios-arm64", "--extra-cflags=-DFT_DEBUG_LOGGING")]), "configure satırı")
    _yaz(os.path.join(ftw, "scripts", "apple", "freetype.sh"), b"export CFLAGS=-DFT_DEBUG_LOGGING\n")
    s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: Next betiğinde", lambda: ft_debug_olcumu(ftw, temiz_env(), []), "scripts/apple/freetype.sh")
    os.remove(ftoption)
    s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: ftoption.h yoksa ölçülemez", lambda: ft_debug_olcumu(ftw, temiz_env(), []), "ftoption.h yok")
    _yaz(ftoption, gercek_satir)
    _sil(os.path.join(ftw, "scripts"))
    s.kirmizi_olmali("FT_DEBUG_LOGGING ölçümü: Next scripts/ yoksa ölçülemez", lambda: ft_debug_olcumu(ftw, temiz_env(), []), "scripts/")
    # --- FreeType brotli yokluğu ölçümü (SK-938-3, SK-938-4): freetype2.pc ve kurulan ftoption.h ---
    pc_temiz = NEXT_FUNCTION_APPLE_SH.split("<<EOF\n")[1].split("EOF\n")[0].replace("Requires.private: zlib, libbrotlidec\n", "Requires.private: zlib\n").replace("\\$", "$").encode()
    ft_yorum = b"/* #define FT_CONFIG_OPTION_USE_BROTLI */\n#define FT_CONFIG_OPTION_SUBPIXEL_RENDERING\n"
    ft_etkin = b"#define FT_CONFIG_OPTION_USE_BROTLI\n"
    bsayac = [0]

    def brotli_kur(pc=pc_temiz, ft=ft_yorum, agac_ft=ft_yorum, ekstra=(), dilim_sayisi=2):
        """Sahte Next iş dizini. pc/ft None ise dosya yazılmaz; agac_ft kaynak ağaçtaki kopyadır (ölçüm ona bakmamalı)."""
        bsayac[0] += 1
        w = os.path.join(kok, "bo-%d" % bsayac[0])
        _yaz(os.path.join(agac_yolu(w, "freetype"), "include", "freetype", "config", "ftoption.h"), agac_ft)
        for d in DILIMLER[:dilim_sayisi]:
            _yaz(os.path.join(w, "prebuilt", d, "pkgconfig", "libpng.pc"), b"Name: libpng\nVersion: 1.6\n")
            if pc is not None:
                _yaz(os.path.join(w, "prebuilt", d, "pkgconfig", "freetype2.pc"), pc)
            if ft is not None:
                _yaz(os.path.join(w, "prebuilt", d, "freetype", "include", "freetype2", "freetype", "config", "ftoption.h"), ft)
        for goreli, veri in ekstra:
            _yaz(os.path.join(w, *goreli.split("/")), veri)
        return w

    bw = brotli_kur()
    bol = freetype_brotli_olcumu(bw)
    s.tamam("brotli ölçümü: temiz iki dilimde geçer; satır sha256 ve dilim adlarını taşır",
            bol[0] == "freetype-brotli-olcum" and hashlib.sha256(pc_temiz).hexdigest()[:12] in bol[1] and hashlib.sha256(ft_yorum).hexdigest()[:12] in bol[1]
            and all(d in bol[1] for d in DILIMLER) and "4 .pc dosyasında" in bol[1])
    s.tamam("brotli ölçümü: kaynak ağaçtaki ftoption.h etkin olsa da kurulan temizse geçer (ölçüm ağaç kopyasına bakmaz)",
            freetype_brotli_olcumu(brotli_kur(agac_ft=ft_etkin))[0] == "freetype-brotli-olcum")
    s.tamam("brotli ölçümü: yama 06 sonrası gerçek blok .pc olarak geçer", b"Requires.private: zlib\n" in pc_temiz and b"brotli" not in pc_temiz.lower())
    for ad_, pc_, parca_ in (
            ("libbrotlidec Requires.private'ta", pc_temiz.replace(b"Requires.private: zlib\n", b"Requires.private: zlib, libbrotlidec\n"), "yalnız zlib değil"),
            ("Requires.private satırı iki kez", pc_temiz + b"Requires.private: zlib\n", "2 kez"),
            ("Requires.private satırı yok", pc_temiz.replace(b"Requires.private: zlib\n", b""), "0 kez"),
            ("Requires.private zlib dışında başka paket", pc_temiz.replace(b"Requires.private: zlib\n", b"Requires.private: zlib, libpng\n"), "yalnız zlib değil"),
            ("brotli başka yerde (Description)", pc_temiz.replace(b"portable font engine.", b"portable Brotli font engine."), "brotli geçen .pc")):
        s.kirmizi_olmali("brotli ölçümü (a): freetype2.pc " + ad_, lambda: freetype_brotli_olcumu(brotli_kur(pc=pc_)), parca_)
    s.kirmizi_olmali("brotli ölçümü (a): başka bir .pc'de Brotli (büyük harf)",
                     lambda: freetype_brotli_olcumu(brotli_kur(ekstra=[("prebuilt/%s/pkgconfig/fontconfig.pc" % DILIMLER[0], b"Requires: Brotli\n")])), "brotli geçen .pc")
    s.kirmizi_olmali("brotli ölçümü (a): iç içe dizindeki .pc'de libbrotlidec",
                     lambda: freetype_brotli_olcumu(brotli_kur(ekstra=[("prebuilt/%s/freetype/lib/pkgconfig/freetype2.pc" % DILIMLER[0], b"Requires.private: libbrotlidec\n")])), "brotli geçen .pc")
    s.kirmizi_olmali("brotli ölçümü (a): büyük harfli uzantılı .PC'de brotli (uzantı büyük-küçük harf duyarsız taranır)",
                     lambda: freetype_brotli_olcumu(brotli_kur(ekstra=[("prebuilt/%s/pkgconfig/x.PC" % DILIMLER[0], b"Requires: libbrotlidec\n")])), "brotli geçen .pc")
    s.kirmizi_olmali("brotli ölçümü (a): freetype2.pc hiç yoksa atlanmaz", lambda: freetype_brotli_olcumu(brotli_kur(pc=None)), "freetype2.pc bulunamadı")
    s.kirmizi_olmali("brotli ölçümü (a): yalnız bir dilimde bozuk .pc yeterli",
                     lambda: freetype_brotli_olcumu(brotli_kur(ekstra=[("prebuilt/%s/pkgconfig/freetype2.pc" % DILIMLER[1], pc_temiz + b"Requires.private: zlib\n")])), "2 kez")
    s.kirmizi_olmali("brotli ölçümü: prebuilt/ hiç yok", lambda: freetype_brotli_olcumu(os.path.join(kok, "bo-yok")), "prebuilt/ yok")
    for ad_, ft_, agac_ in (("etkin #define (kurulu etkin, ağaç kopyası temiz)", ft_etkin, ft_yorum), ("girintili #  define", b"  #  define FT_CONFIG_OPTION_USE_BROTLI 1\n", ft_yorum),
                            ("tab ile", b"x\n#\tdefine\tFT_CONFIG_OPTION_USE_BROTLI\n", ft_yorum),
                            ("dosya sonu satırsız", b"x\n#define FT_CONFIG_OPTION_USE_BROTLI", ft_yorum)):
        s.kirmizi_olmali("brotli ölçümü (b): kurulan ftoption.h " + ad_, lambda: freetype_brotli_olcumu(brotli_kur(ft=ft_, agac_ft=agac_)), "etkin tanımlı")
    s.kirmizi_olmali("brotli ölçümü (b): yalnız bir dilimde etkin",
                     lambda: freetype_brotli_olcumu(brotli_kur(ekstra=[("prebuilt/%s/freetype/include/freetype2/freetype/config/ftoption.h" % DILIMLER[1], ft_etkin)])), "etkin tanımlı")
    s.kirmizi_olmali("brotli ölçümü (b): kurulan ftoption.h hiç yok (ağaçtaki kopya yetmez)", lambda: freetype_brotli_olcumu(brotli_kur(ft=None)), "ftoption.h bulunamadı")
    s.tamam("check_tam: freetype_brotli_olcumu'nu da çağırır", "freetype_brotli_olcumu" in cagri_sirasi("check_tam"))
    s.tamam("derleme env: izin listesi boş liste verir", env_sorunlari(dict((a, "x") for a in ENV_IZINLI)) == [] and set(temiz_env()) <= ENV_IZINLI)
    # SK-941-6: izin listesi herkese açık tanı artifact'ının tek ortam denetimidir; ad eklemek security turu gerektirir (SK-941-5).
    s.tamam("derleme env: izin listesi tam eşitlikle sekiz ad",
            ENV_IZINLI == frozenset(["PATH", "HOME", "TMPDIR", "LANG", "GIT_TERMINAL_PROMPT", "DEVELOPER_DIR", "ACLOCAL_PATH", "SED"]))
    s.tamam("derleme env: PKG_CONFIG_PATH adıyla kırmızı", any("tanımlı olmamalı" in x for x in env_sorunlari(dict(temiz_env(), PKG_CONFIG_PATH="/opt/homebrew/lib/pkgconfig"))))
    s.tamam("derleme env: izinli olmayan değişken kırmızı", any("izinli olmayan" in x for x in env_sorunlari(dict(temiz_env(), CFLAGS="-O2"))))
    eski_pc = os.environ.get("PKG_CONFIG_PATH")
    os.environ["PKG_CONFIG_PATH"] = "/opt/homebrew/lib/pkgconfig"
    try:
        s.tamam("derleme env: ortamdaki PKG_CONFIG_PATH alt sürece geçmez (temiz_env miras almaz)", "PKG_CONFIG_PATH" not in temiz_env())
    finally:
        if eski_pc is None:
            del os.environ["PKG_CONFIG_PATH"]
        else:
            os.environ["PKG_CONFIG_PATH"] = eski_pc
    s.tamam("derleme_env izin listesini kendisi de denetler", "env_sorunlari" in cagri_sirasi("derleme_env"))
    for islev in ("hepsi", "asama_check", "asama_package"):
        s.tamam("%s: check_tam kullanır (FreeType ölçümü atlanamaz)" % islev, "check_tam" in cagri_sirasi(islev) and "check" not in cagri_sirasi(islev))
    sira_ct = cagri_sirasi("check_tam")
    s.tamam("check_tam: check'ten sonra ft_debug_olcumu", sira_ct.index("check") < sira_ct.index("ft_debug_olcumu"))
    sira_h = cagri_sirasi("hepsi")
    s.tamam("hepsi: derleme sonrası kilit denetiminden sonra check_tam, paketten önce", sira_h.index("kilit_dogrula", sira_h.index("derle")) < sira_h.index("check_tam") < sira_h.index("paketle"))

    # --- guvenli_ac ---
    def zip_dene(ad, parca, **kw):
        sayac[0] += 1
        zp = os.path.join(kok, "z-%d.zip" % sayac[0])
        with zipfile.ZipFile(zp, "w") as z:
            for girdi in kw.get("girdiler", []):
                zi = zipfile.ZipInfo("x")
                zi.filename = girdi[0]     # ZipInfo.__init__ Windows'ta ters bölüyü çevirir; ham ad burada atanır
                zi.external_attr = girdi[2] << 16 if len(girdi) > 2 else (0o100644 << 16)
                z.writestr(zi, girdi[1])
        s.kirmizi_olmali("zip: " + ad, lambda: guvenli_ac(zp, os.path.join(kok, "za-%d" % sayac[0]), **kw.get("sinir", {})), parca)

    zip_dene("mutlak yol", "güvensiz", girdiler=[("/etc/x", b"x")])
    zip_dene("üst dizin", "güvensiz", girdiler=[("a/../b", b"x")])
    if os.sep == "/":
        # Windows'ta zipfile okurken ters bölüyü yol ayırıcıya çevirir (sonra ".." denetimi yakalar); ham ad denetimi POSIX'te sınanır.
        zip_dene("ters bölü", "güvensiz", girdiler=[("a\\b", b"x")])
    zip_dene("ters bölü ile üst dizin", "güvensiz", girdiler=[("a\\..\\..\\b", b"x")])
    zip_dene("iki nokta (NTFS akışı)", "güvensiz", girdiler=[("a:b", b"x")])
    zip_dene("ayrılmış ad", "ayrılmış", girdiler=[("a/NUL.txt", b"x")])
    zip_dene("sonda nokta", "güvensiz", girdiler=[("a/b.", b"x")])
    zip_dene("sonda boşluk", "güvensiz", girdiler=[("a/b ", b"x")])
    zip_dene("katlama çakışması", "çakışan", girdiler=[("a/B", b"x"), ("a/b", b"y")])
    zip_dene("sembolik bağ", "sembolik", girdiler=[("a/l", b"b", 0o120777)])
    zip_dene("ASCII dışı", "ASCII", girdiler=[("a/ç", b"x")])
    zip_dene("dosya boyut sınırı", "boyut", girdiler=[("a/b", b"xxxx")], sinir={"dosya_sinir": 2})
    zip_dene("toplam boyut sınırı", "toplam", girdiler=[("a/b", b"xx"), ("a/c", b"xx")], sinir={"toplam_sinir": 3})
    zip_dene("boş zip", "boş", girdiler=[])

    # --- paketleme: bayt sözleşmesi ---
    m = manifest_baytlari(GOLDEN_COMMIT, GOLDEN_DOSYALAR)
    l = liste_baytlari(GOLDEN_COMMIT, GOLDEN_DOSYALAR)
    s.tamam("manifest: LF ile biter, CR yok, anahtarlar sıralı", m.endswith(b"}\n") and b"\r" not in m and list(json.loads(m)) == ["commit", "files", "kind", "schema", "source"])
    s.tamam("manifest: yol sıralı", list(json.loads(m)["files"]) == sorted(GOLDEN_DOSYALAR))
    satirlar_l = l.decode().split("\n")
    s.tamam("liste: beş başlık satırı bu sırada", satirlar_l[:5] == ["# forge-ffmpeg-pin 2", "# kind: vendored-source-build", "# repo: " + NEXT_REPO, "# commit: " + GOLDEN_COMMIT, LISTE_SON_BASLIK])
    s.tamam("liste: LF ile biter, CR yok, satırlar bayt sırasında", l.endswith(b"\n") and b"\r" not in l and satirlar_l[5:-1] == ["%s  %s" % (GOLDEN_DOSYALAR[p], p) for p in sorted(GOLDEN_DOSYALAR, key=lambda x: x.encode())])
    s.tamam("manifest vektörü sabit", hashlib.sha256(m).hexdigest() == GOLDEN_MANIFEST_SHA256)
    s.tamam("liste vektörü sabit", hashlib.sha256(l).hexdigest() == GOLDEN_LISTE_SHA256)

    # --- paketleme: uçtan uca (sahte çerçeve ağacı) ---
    pk = os.path.join(kok, "p1")
    xc = _sahte_xc(pk)
    ffp = _sahte_kume(pk)
    pwork = work1
    pcikti = os.path.join(kok, "pdist")
    pkilit = dict(sahte, bytes=b'{"kilit": 1}\n')
    satirlar_p, kume_p = check(xc, ffp, ot)
    ozet = paketle(xc, pcikti, "deneme-abc1234", pkilit, pwork, ["t1 x/y 000000000000 -> 111111111111"], satirlar_p, kume_p)
    adlar = sorted(os.listdir(pcikti))
    beklenen = sorted([fw + ".xcframework.zip" for fw in CERCEVELER] + ["lgpl-manifest.json", "frameworks.sha256", "source-lock.json", "compiled-set.txt",
                                                                    "configure-lines.txt", "toolchain.txt", "source-edits.diff", "SHA256SUMS"])
    s.tamam("paket: beklenen dosya kümesi", adlar == beklenen)
    sums = open(os.path.join(pcikti, "SHA256SUMS"), "rb").read().decode()
    s.tamam("paket: SHA256SUMS her dosya için doğru ve kendini içermez",
            all(("%s  %s" % (sha256_dosya(os.path.join(pcikti, a)), a)) in sums.splitlines() for a in adlar if a != "SHA256SUMS") and "SHA256SUMS" not in sums.replace("SHA256SUMS_", ""))
    s.tamam("paket: özet SHA256SUMS dosyasının SHA-256'sı", ozet == sha256_dosya(os.path.join(pcikti, "SHA256SUMS")))
    s.tamam("paket: SHA256SUMS yeniden yazılınca aynı kalır (kendini içermez)", sha256sums_yaz(pcikti) == ozet)
    pm = json.loads(open(os.path.join(pcikti, "lgpl-manifest.json"), "rb").read())
    s.tamam("paket: manifest source/commit Next'ten", pm["source"] == NEXT_REPO and pm["commit"] == sahte["sources"][0]["commit"] and pm["kind"] == MANIFEST_KIND)
    s.tamam("paket: strip-frameworks.sh ve SOURCE manifestte yok", not any(p.endswith("/strip-frameworks.sh") or p.endswith("/SOURCE") for p in pm["files"]))
    s.tamam("paket: manifest girdileri zip içeriğiyle aynı özet", all(len(v["sha256"]) == 64 for v in pm["files"].values()) and len(pm["files"]) >= 8 * 2 + 8)
    with zipfile.ZipFile(os.path.join(pcikti, "ffmpegkit.xcframework.zip")) as z:
        s.tamam("paket: zip girdileri SOURCE/strip içermez", not any(n.endswith("/SOURCE") or n.endswith("strip-frameworks.sh") for n in z.namelist()))
        s.tamam("paket: zip girdi adları xcframework kökünden", all(n.startswith("ffmpegkit.xcframework/") for n in z.namelist()))
    s.tamam("paket: kaynak xc ağacı değişmedi (silme kopyada)", os.path.isfile(os.path.join(xc, "ffmpegkit.xcframework", DILIMLER[0], "ffmpegkit.framework", "SOURCE")))
    s.tamam("paket: source-lock.json kilit baytları", open(os.path.join(pcikti, "source-lock.json"), "rb").read() == b'{"kilit": 1}\n')
    s.tamam("paket: source-edits.diff başlıklı", open(os.path.join(pcikti, "source-edits.diff"), "rb").read().startswith(b"### ffmpeg-kit-next"))
    tc = open(os.path.join(pcikti, "toolchain.txt"), "rb").read().decode()
    s.tamam("paket: toolchain.txt yama satırlarını taşır, sabit libpng cümlesini taşımaz",
            "\n  t1 x/y 000000000000 -> 111111111111\n" in tc and "libpng fp.h yaması" not in tc and "v1.6.58" not in tc)
    zip_yaz(xc, "libavutil.xcframework", os.path.join(kok, "d1.zip"))
    zip_yaz(xc, "libavutil.xcframework", os.path.join(kok, "d2.zip"))
    s.tamam("paket: zip yazımı tekrarlanabilir (aynı girdi, aynı bayt)", open(os.path.join(kok, "d1.zip"), "rb").read() == open(os.path.join(kok, "d2.zip"), "rb").read())
    if os.name == "posix":
        exe = os.path.join(xc, "libavutil.xcframework", DILIMLER[0], "libavutil.framework", "libavutil")
        os.chmod(exe, 0o755)
        zp = os.path.join(kok, "exe.zip")
        zip_yaz(xc, "libavutil.xcframework", zp)
        with zipfile.ZipFile(zp) as z:
            kip = dict((zi.filename, (zi.external_attr >> 16) & 0o777) for zi in z.infolist())
        s.tamam("zip: kip bitleri korunur", kip["libavutil.xcframework/ios-arm64/libavutil.framework/libavutil"] == 0o755 and kip["libavutil.xcframework/Info.plist"] == 0o644)
        os.symlink("Info.plist", os.path.join(xc, "libavutil.xcframework", "baglanti"))
        s.kirmizi_olmali("paket: sembolik bağ", lambda: zip_yaz(xc, "libavutil.xcframework", os.path.join(kok, "sym.zip")), "sembolik")
        os.remove(os.path.join(xc, "libavutil.xcframework", "baglanti"))
    bosluk = _sahte_xc(os.path.join(kok, "p2"), ekstra={"libavutil.xcframework/ios-arm64/libavutil.framework/a b": b"x"})
    s.kirmizi_olmali("paket: yolda boşluk", lambda: paketle(bosluk, os.path.join(kok, "pdist2"), "deneme-abc1234", pkilit, pwork, [], satirlar_p, kume_p), "ASCII")

    # --- python -O altında aynı çıkış kodu ---
    lp2 = os.path.join(kok, "k1", "lock-kayma.json")
    _kilit_dosyasi(lp2, sahte)
    for ad, ek_env in (("python -O", {}), ("PYTHONOPTIMIZE=1", {"PYTHONOPTIMIZE": "1"})):
        env = dict(os.environ, **ek_env)
        komut = [sys.executable] + (["-O"] if ad == "python -O" else []) + [os.path.abspath(__file__), "verify-lock", "--work", work, "--lock", lp2, "--asama", "sonra", "--allow-file"]
        p = subprocess.run(komut, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        s.tamam("%s altında bozuk ağaç çıkış kodu 1" % ad, p.returncode == 1 and b"KIRMIZI" in p.stderr)
    p = subprocess.run([sys.executable, os.path.abspath(__file__), "verify-lock", "--work", kok, "--lock", lp2, "--asama", "yanlis"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    s.tamam("kullanım hatası çıkış kodu 2", p.returncode == 2)
    _selftest_tani(s, kok)
    _selftest_bekci(s, kok)


def _yml_adim_blogu(yml, ad):
    """build.yml'deki adımın bütün metni (bir sonraki `- name:` satırına kadar); adım tam bir kez bulunmalı."""
    parcalar = re.split(r"(?m)^      - name: ", yml)
    uygun = [p for p in parcalar[1:] if p.split("\n", 1)[0].strip() == ad]
    kirmizi_if(len(uygun) != 1, "build.yml: %s adımı tam bir kez bulunmalı" % ad)
    return uygun[0]


def _yml_is_izinleri(yml, is_adi):
    """build.yml'de işin `permissions` girdileri (örn. ["contents: read"]); iş ve blok tam bir kez bulunmalı, satır içi biçim kabul edilmez."""
    satirlar = yml.split("\n")
    bas = [i for i, l in enumerate(satirlar) if l == "  %s:" % is_adi]
    kirmizi_if(len(bas) != 1, "build.yml: %s işi tam bir kez bulunmalı" % is_adi)
    govde = []
    for l in satirlar[bas[0] + 1:]:
        if re.match(r"\S|  \S", l):
            break
        govde.append(l)
    idx = [i for i, l in enumerate(govde) if l == "    permissions:"]
    kirmizi_if(len(idx) != 1, "build.yml: %s işinde permissions bloğu tam bir kez bulunmalı" % is_adi)
    girdiler = []
    for l in govde[idx[0] + 1:]:
        if not l.startswith("      "):
            break
        girdiler.append(l.strip())
    return girdiler


def _selftest_tani(s, kok):
    """Derleme bekçisi, günlük gizleme ve tanı toplama (SK-893/936/938 kararları değişmeden; bunlar yalnız görünürlük ekler)."""
    sahte_token = "gh" + "p_" + "a" * 36
    sahte_pat = "github" + "_pat_" + "B" * 30

    # --- gizleme ---
    for ad, girdi, yasak, beklenen in (
            ("URL kullanıcı:parola", "fatal: unable to access 'https://u:parola1@h.example/x'", "parola1", "://***@"),
            ("GitHub belirteci", "x " + sahte_token + " y", sahte_token, "***"),
            ("ince taneli belirteç", "x " + sahte_pat + " y", sahte_pat, "***"),
            ("Authorization başlığı", "Authorization: Basic dXNlcjpwYXJvbGE=", "dXNlcjpwYXJvbGE", "Authorization: ***"),
            ("Bearer", "header bearer abcdefgh12345678 x", "abcdefgh12345678", "bearer ***"),
            ("GITHUB_TOKEN değişkeni", "GITHUB_TOKEN=cok-gizli", "cok-gizli", "GITHUB_TOKEN=***"),
            ("küçük harfli api_key", "my_api" + "_key=cok-gizli", "cok-gizli", "my_api_key=***"),
            ("PASSWORD değişkeni", "DB_PASSWORD=cok-gizli", "cok-gizli", "DB_PASSWORD=***")):
        cikti = gizle(girdi)
        s.tamam("gizle: %s gizlenir" % ad, yasak not in cikti and beklenen in cikti)
    for zararsiz in ("PATH=/usr/bin:/bin", "CC=clang", "INFO: Skipping freetype, dependencies built=0, already built=", "HOME=/Users/runner",
                     "https://github.com/arthenica/libexpat", "AUTHORS file"):
        s.tamam("gizle: zararsız satır aynen kalır (%s)" % zararsiz[:30], gizle(zararsiz) == zararsiz)
    s.tamam("gizle: satır sonuna taşmaz", gizle("A_TOKEN=x\nPATH=/bin\n") == "A_TOKEN=***\nPATH=/bin\n")

    # --- git stderr özeti ---
    s.tamam("git_hata_ozeti: boş stderr 'çıktı yok'", git_hata_ozeti(b"") == "çıktı yok")
    s.tamam("git_hata_ozeti: son üç satır", git_hata_ozeti(b"a\nb\nc\nd\n\n") == "b | c | d")
    s.tamam("git_hata_ozeti: 300 karakterle sınırlı", len(git_hata_ozeti(b"x" * 2000)) == 300)
    s.tamam("git_hata_ozeti: URL parolası gizli", "parola1" not in git_hata_ozeti(b"fatal: https://u:parola1@h.example/x"))

    asil_git = globals()["git"]
    try:
        globals()["git"] = lambda args, cwd=None, dosya_izni=False, girdi=None, zaman=900: (128, b"", b"fatal: https://u:parola1@h.example/x\n")
        try:
            git_ok(["status"])
            mesaj = ""
        except Kirmizi as e:
            mesaj = str(e)
    finally:
        globals()["git"] = asil_git
    s.tamam("git_ok: hata mesajında URL parolası gizli", "başarısız" in mesaj and "parola1" not in mesaj and "https://***@h.example/x" in mesaj)
    kilit_k = {"name": "expat", "url": "https://github.com/arthenica/libexpat", "ref": "R_2_9_0", "commit": "a" * 40}
    durum = {"basarisiz": "commit"}

    def sahte_git(args, cwd=None, dosya_izni=False, girdi=None, zaman=900):
        if args[0] == "fetch":
            etiket_mi = any(a.startswith("+refs/tags/") for a in args)
            if durum["basarisiz"] == "commit" and not etiket_mi:
                return 128, b"", b"fatal: could not read from https://u:parola1@h.example/x\n"
            if durum["basarisiz"] == "etiket" and etiket_mi:
                return 128, b"", b"fatal: couldn't find remote ref refs/tags/R_2_9_0\n"
            return 0, b"", b""
        if args[0] == "rev-parse":
            return 0, b"a" * 40 + b"\n", b""
        return 0, b"", b""

    mesajlar = {}
    try:
        globals()["git"] = sahte_git
        for hangisi in ("commit", "etiket"):
            durum["basarisiz"] = hangisi
            try:
                kaynak_cek(kilit_k, os.path.join(kok, "tani-kaynak-" + hangisi), False)
                mesajlar[hangisi] = ""
            except Kirmizi as e:
                mesajlar[hangisi] = str(e)
    finally:
        globals()["git"] = asil_git
    s.tamam("kaynak_cek: commit indirilemezse git stderr'i mesajda, parola gizli",
            "indirilemedi" in mesajlar["commit"] and "(git: fatal: could not read from https://***@h.example/x)" in mesajlar["commit"] and "parola1" not in mesajlar["commit"])
    s.tamam("kaynak_cek: etiket getirilemezse git stderr'i mesajda", "getirilemedi" in mesajlar["etiket"] and "(git: fatal: couldn't find remote ref" in mesajlar["etiket"])
    try:
        kaynak_cek({"name": "x", "url": "file://" + os.path.join(kok, "yok-depo").replace(os.sep, "/"), "ref": "r", "commit": "b" * 40},
                   os.path.join(kok, "tani-kaynak-3"), True)
        mesaj = ""
    except Kirmizi as e:
        mesaj = str(e)
    s.tamam("kaynak_cek: gerçek git hatası mesaja girer (boş değil)", "(git: " in mesaj and "(git: çıktı yok)" not in mesaj)

    # --- build.log kuyruğu ---
    buyuk = os.path.join(kok, "buyuk.log")
    with open(buyuk, "wb") as f:
        for i in range(60000):
            f.write(b"satir %06d dolgu dolgu dolgu\n" % i)
        f.write(b"INFO: Skipping libass, dependencies built=0, already built=\nINFO: Skipping freetype, dependencies built=0, already built=\nSON\n")
    sonu = log_sonu(buyuk, 1000)
    s.tamam("log_sonu: bayt sınırını aşmaz, tam satırla başlar", len(sonu) <= 1000 and sonu.startswith("satir "))
    s.tamam("log_sonu: son satır korunur", sonu.endswith("SON\n") and "satir 000000" not in sonu)
    s.tamam("log_sonu: küçük dosya aynen", log_sonu(buyuk, 10 ** 9).startswith("satir 000000"))
    s.tamam("bekleyen_kitapliklar: sıralı ve yinelenmez", bekleyen_kitapliklar("INFO: Skipping libass, dependencies built=0\nINFO: Skipping freetype, dependencies built=0\nINFO: Skipping libass, dependencies built=0\nINFO: Skipping lame, dependencies built=1\n") == ["freetype", "libass"])

    # --- tanı toplama ---
    cwork = os.path.join(kok, "tani-work")
    _yaz(os.path.join(cwork, "build.log"), b"BASLA TOKEN_X=cok-gizli\n" + b"dolgu dolgu dolgu dolgu\n" * 400000 + b"INFO: Skipping freetype, dependencies built=0, already built=\nSON\n")
    _yaz(os.path.join(cwork, "src", "expat", "expat", "config.log"), b"configure basladi\n")
    _yaz(os.path.join(cwork, "src", "ffmpeg", "build", "meson-logs", "meson-log.txt"), b"eski satir\n" * 100000 + b"meson son\n")
    _yaz(os.path.join(cwork, "src", "foo", "baska.txt"), b"alinmaz\n")
    ffm = os.path.join(cwork, "src", "ffmpeg")
    ffmpeg_adlari = ["libavformat/protocol_list.c", "libavformat/demuxer_list.c", "libavformat/muxer_list.c", "libavcodec/codec_list.c", "libavcodec/parser_list.c",
                     "libavcodec/bsf_list.c", "libavfilter/filter_list.c", "libavdevice/indev_list.c", "libavdevice/outdev_list.c", "ffbuild/config.fate", "config.h"]
    s.tamam("TANI_FFMPEG_DOSYALARI: SK-943-7'deki 11 tam ad (elle yazılmış liste)", sorted(TANI_FFMPEG_DOSYALARI) == sorted(ffmpeg_adlari))
    for goreli_ in ffmpeg_adlari:
        _yaz(os.path.join(ffm, *goreli_.split("/")), ("içerik " + goreli_ + "\n").encode())
    _yaz(os.path.join(ffm, "config.h"), b"#define FFMPEG_CONFIGURATION \"--prefix=/x --enable-version3\"\nAPI_TOKEN=cok-gizli\n")
    _yaz(os.path.join(ffm, "ffbuild", "config.mak"), b"alinmaz\n")
    _yaz(os.path.join(ffm, "libavformat", "allformats.c"), b"alinmaz\n")
    _yaz(os.path.join(ffm, "libavformat", "demuxer_list.c.bak"), b"alinmaz\n")
    _yaz(os.path.join(cwork, "prebuilt", "arm64-apple-ios", "libpng", "x"), b"x")
    _yaz(os.path.join(cwork, "prebuilt", "arm64-apple-ios", "expat", "x"), b"x")
    _yaz(os.path.join(cwork, ".tmp", "gas-preprocessor.pl"), b"cok-gizli-betik")
    _yaz(os.path.join(cwork, ".tmp", "source", "config", "alt-dizin-dosyasi"), b"x")
    _yaz(os.path.join(cwork, ".tmp", "cmake", "build", "alt-dizin-dosyasi"), b"x")
    _yaz(os.path.join(cwork, ".tmp", ".gizli"), b"x")
    if os.name == "posix":
        os.symlink(os.path.join(cwork, "build.log"), os.path.join(cwork, "src", "foo", "config.log"))
    cout = os.path.join(kok, "tani-out")
    s.tamam("tani_topla: çıkış kodu 0", tani_topla(cwork, cout) == 0)
    s.tamam("tani_topla: yalnız beklenen dosyalar",
            sorted(os.listdir(cout)) == sorted(["build.log", "indeks.txt", "prebuilt-liste.txt", "tmp-liste.txt", "src__expat__expat__config.log", "src__ffmpeg__build__meson-logs__meson-log.txt"]
                                               + ["src__ffmpeg__" + g.replace("/", "__") for g in ffmpeg_adlari]))
    with open(os.path.join(cout, "src__ffmpeg__libavdevice__outdev_list.c"), "rb") as f:
        s.tamam("tani_topla: FFmpeg küme listesi içeriği aynen", f.read() == b"i\xc3\xa7erik libavdevice/outdev_list.c\n")
    with open(os.path.join(cout, "src__ffmpeg__config.h"), "rb") as f:
        ffconfig_cikti = f.read()
    s.tamam("tani_topla: config.h alınır ve gizlenir", b"--enable-version3" in ffconfig_cikti and b"cok-gizli" not in ffconfig_cikti and b"API_TOKEN=***" in ffconfig_cikti)
    with open(os.path.join(cout, "build.log"), "rb") as f:
        log_cikti = f.read()
    s.tamam("tani_topla: build.log sınırlı (baş+son), atlanan bayt yazılı", len(log_cikti) <= TANI_LOG_BAS + TANI_LOG_SON + 200 and b"bayt atland" in log_cikti)
    s.tamam("tani_topla: baş ve son korunur", log_cikti.startswith(b"BASLA") and log_cikti.endswith(b"SON\n"))
    s.tamam("tani_topla: gizli değer artifact'a girmez", b"cok-gizli" not in log_cikti and b"TOKEN_X=***" in log_cikti)
    s.tamam("tani_topla: günlük dosyası sondan sınırlı", os.path.getsize(os.path.join(cout, "src__ffmpeg__build__meson-logs__meson-log.txt")) <= TANI_DOSYA_SON + 100)
    with open(os.path.join(cout, "src__ffmpeg__build__meson-logs__meson-log.txt"), "rb") as f:
        s.tamam("tani_topla: günlük dosyasının sonu korunur", f.read().endswith(b"meson son\n"))
    with open(os.path.join(cout, "indeks.txt"), "rb") as f:
        indeks = f.read().decode()
    s.tamam("tani_topla: indeks bekleyen kütüphaneyi söyler", "bağımlılığı bekleyen kütüphaneler: freetype" in indeks)
    with open(os.path.join(cout, "prebuilt-liste.txt"), "rb") as f:
        s.tamam("tani_topla: prebuilt listesi", b"prebuilt/arm64-apple-ios/libpng" in f.read())
    with open(os.path.join(cout, "tmp-liste.txt"), "rb") as f:
        tmp_liste = f.read()
    s.tamam("tani_topla: .tmp/ üst düzey listesi ad + lstat türü, sıralı; alt dizin ve içerik yok",
            tmp_liste.decode("utf-8").splitlines() == ["dosya\t.gizli", "dizin\tcmake", "dosya\tgas-preprocessor.pl", "dizin\tsource"] and tmp_liste.endswith(b"\n")
            and b"alt-dizin-dosyasi" not in tmp_liste and b"cok-gizli" not in tmp_liste)
    s.kirmizi_olmali("tani_topla: çıktı dizini varsa kırmızı", lambda: tani_topla(cwork, cout), "zaten var")
    _yaz(os.path.join(cwork, "src", "ffmpeg", "libavcodec", "codec_list.c"), b"x" * (TANI_DOSYA_SON + 5000) + b"SON\n")
    buyuk_out = os.path.join(kok, "tani-buyuk")
    tani_topla(cwork, buyuk_out)
    s.tamam("tani_topla: büyük FFmpeg dosyası TANI_DOSYA_SON ile sınırlı, atlanan bayt yazılı",
            TANI_DOSYA_SON <= os.path.getsize(os.path.join(buyuk_out, "src__ffmpeg__libavcodec__codec_list.c")) <= TANI_DOSYA_SON + 100)
    with open(os.path.join(buyuk_out, "src__ffmpeg__libavcodec__codec_list.c"), "rb") as f:
        s.tamam("tani_topla: büyük FFmpeg dosyasının başı korunur", f.read(10) == b"x" * 10)
    s.tamam("tani_topla: çalışma dizini yoksa 0 ve çıktı oluşmaz", tani_topla(os.path.join(kok, "yok-work"), os.path.join(kok, "yok-out")) == 0 and not os.path.exists(os.path.join(kok, "yok-out")))
    for ad, kur_, beklenen_ in (("yok", lambda w: os.makedirs(w), ".tmp: yok\n"), ("dosya", lambda w: _yaz(os.path.join(w, ".tmp"), b"x"), ".tmp: dizin değil (dosya)\n")):
        tw = os.path.join(kok, "tani-tmp-" + ad)
        kur_(tw)
        to = os.path.join(kok, "tani-tmp-out-" + ad)
        tani_topla(tw, to)
        with open(os.path.join(to, "tmp-liste.txt"), "rb") as f:
            s.tamam("tani_topla: .tmp %s ise liste bunu yazar (hata vermez)" % ad, f.read().decode("utf-8") == beklenen_)
    if os.name == "posix":
        bag_work = os.path.join(kok, "tani-bag-work")
        _yaz(os.path.join(kok, "tani-bag-hedef"), b"gizli-icerik\n")
        os.makedirs(bag_work)
        os.symlink(os.path.join(kok, "tani-bag-hedef"), os.path.join(bag_work, "build.log"))
        bag_out = os.path.join(kok, "tani-bag-out")
        tani_topla(bag_work, bag_out)
        s.tamam("tani_topla: build.log sembolik bağsa izlenmez", not os.path.exists(os.path.join(bag_out, "build.log")))
        # FFmpeg dosyası sembolik bağ ya da yolun bir dizini sembolik bağ ise izlenmez; indekste yazılır.
        ffbag_work = os.path.join(kok, "tani-ffbag-work")
        _yaz(os.path.join(kok, "tani-ffbag-hedef", "config.h"), b"gizli-icerik\n")
        _yaz(os.path.join(kok, "tani-ffbag-hedef", "libavformat", "demuxer_list.c"), b"gizli-icerik\n")
        _yaz(os.path.join(ffbag_work, "src", "ffmpeg", "libavcodec", "codec_list.c"), b"gercek\n")
        os.symlink(os.path.join(kok, "tani-ffbag-hedef", "config.h"), os.path.join(ffbag_work, "src", "ffmpeg", "config.h"))
        os.symlink(os.path.join(kok, "tani-ffbag-hedef", "libavformat"), os.path.join(ffbag_work, "src", "ffmpeg", "libavformat"))
        ffbag_out = os.path.join(kok, "tani-ffbag-out")
        tani_topla(ffbag_work, ffbag_out)
        with open(os.path.join(ffbag_out, "indeks.txt"), "rb") as f:
            ffbag_indeks = f.read().decode()
        s.tamam("tani_topla: sembolik bağlı FFmpeg dosyası ve dizini izlenmez, yalnız gerçek dosya alınır",
                sorted(d for d in os.listdir(ffbag_out) if d.startswith("src__")) == ["src__ffmpeg__libavcodec__codec_list.c"]
                and "src__ffmpeg__config.h: yok ya da sembolik bağ" in ffbag_indeks and "gizli-icerik" not in ffbag_indeks)
    eski_toplam = globals()["TANI_TOPLAM"]
    try:
        globals()["TANI_TOPLAM"] = 1
        sinir_out = os.path.join(kok, "tani-sinir")
        tani_topla(cwork, sinir_out)
    finally:
        globals()["TANI_TOPLAM"] = eski_toplam
    with open(os.path.join(sinir_out, "indeks.txt"), "rb") as f:
        s.tamam("tani_topla: toplam sınırı aşılınca günlük dosyaları alınmaz ve indekste yazar",
                b"toplam s" in f.read() and not os.path.exists(os.path.join(sinir_out, "src__expat__expat__config.log")))
    s.tamam("tani_topla: toplam sınırı FFmpeg dosyaları için de geçerli", not os.path.exists(os.path.join(sinir_out, "src__ffmpeg__config.h")))
    eksik_work = os.path.join(kok, "tani-eksik-work")
    _yaz(os.path.join(eksik_work, "src", "ffmpeg", "config.h"), b"x\n")
    eksik_out = os.path.join(kok, "tani-eksik-out")
    tani_topla(eksik_work, eksik_out)
    with open(os.path.join(eksik_out, "indeks.txt"), "rb") as f:
        eksik_indeks = f.read().decode()
    s.tamam("tani_topla: olmayan FFmpeg dosyası indekste 'yok' diye yazılır, çökmez",
            "src__ffmpeg__libavdevice__outdev_list.c: yok ya da sembolik bağ" in eksik_indeks and os.path.isfile(os.path.join(eksik_out, "src__ffmpeg__config.h")))

    # --- iş akışı (build.yml) ---
    yol = os.path.join(HERE, ".github", "workflows", "build.yml")
    if not os.path.isfile(yol):
        return
    with open(yol, "rb") as f:
        yml = f.read().decode("utf-8")
    kullanimlar = re.findall(r"(?m)^\s*(?:- )?uses: (\S+)", yml)
    s.tamam("build.yml: her uses tam commit SHA'sına sabit", len(kullanimlar) >= 4 and all(re.fullmatch(r"[A-Za-z0-9_./-]+@[0-9a-f]{40}", u) for u in kullanimlar))
    kod_satirlari = "\n".join(l for l in yml.split("\n") if not l.strip().startswith("#"))
    s.tamam("build.yml: secrets ve cache yok", "secrets." not in kod_satirlari and "actions/cache" not in kod_satirlari)
    # SK-941-6: tanı artifact'ı herkese açık sayılır; derleme işinin belirteci yalnız okuyabilir (id-token ve yazma izni yok).
    try:
        is_izni = _yml_is_izinleri(yml, "build")
    except Kirmizi as e:
        is_izni = [str(e)]
    s.tamam("build.yml: build işinin izni yalnız contents: read", is_izni == ["contents: read"])
    try:
        topla = _yml_adim_blogu(yml, "Tanı kayıtlarını topla")
        yukle = _yml_adim_blogu(yml, "Tanı kayıtlarını yükle")
        asil_yukle = _yml_adim_blogu(yml, "Çıktıları yükle")
        derle_adimi = _yml_adim_blogu(yml, "Derle")
    except Kirmizi as e:
        s.tamam("build.yml tanı adımları bulunur: %s" % e, False)
        return
    kosul = "if: failure() || cancelled()"
    s.tamam("build.yml: tanı topla adımı başarısızlıkta ve iptalde koşar", kosul in topla and "build.py tani" in topla)
    s.tamam("build.yml: tanı yükle adımı başarısızlıkta ve iptalde koşar", kosul in yukle)
    s.tamam("build.yml: tanı adımları Derle'den sonra", yml.index("- name: Tanı kayıtlarını topla") > yml.index("- name: Derle") and yml.index("- name: Tanı kayıtlarını yükle") > yml.index("- name: Tanı kayıtlarını topla"))
    s.tamam("build.yml: tanı yükleme mevcut upload-artifact SHA'sını kullanır",
            re.search(r"uses: (\S+)", yukle).group(1) == re.search(r"uses: (\S+)", asil_yukle).group(1))
    s.tamam("build.yml: tanı artifact'ı kısa saklamalı, dosya yoksa sessiz", "retention-days: 3" in yukle and "if-no-files-found: ignore" in yukle)
    s.tamam("build.yml: tanı artifact'ı koşuya özel ad ve yalnız tani dizini", "format('tani-{0}', github.run_id)" in yukle and "runner.temp }}/tani/" in yukle)
    s.tamam("build.yml: Derle adımı çıktıyı tamponlamaz (PYTHONUNBUFFERED)", "PYTHONUNBUFFERED: '1'" in derle_adimi)
    s.tamam("build.yml: tani build.py'nin gerçek bir alt komutu", ayristirici().parse_args(["tani", "--work", "w", "--out", "o"]).komut == "tani")
    if os.name == "posix" and shutil.which("bash") and shutil.which("python3"):
        gorev = os.path.join(kok, "yml-tani")
        os.makedirs(gorev)
        shutil.copy(os.path.join(HERE, "build.py"), os.path.join(gorev, "build.py"))
        _yaz(os.path.join(gorev, "tmp", "work", "build.log"), b"BASLA\nINFO: Skipping libass, dependencies built=0\n")
        betik = re.search(r"(?m)^        run: (.+)$", topla).group(1)
        p = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", betik], cwd=gorev, env=dict(os.environ, RUNNER_TEMP=os.path.join(gorev, "tmp")),
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
        s.tamam("build.yml: tanı topla adımı gerçek bash'te çalışır ve tani dizinini üretir",
                p.returncode == 0 and os.path.isfile(os.path.join(gorev, "tmp", "tani", "build.log")))


def _selftest_bekci(s, kok):
    """zamanli_calistir ve derle bekçisi (yalnız POSIX: süreç grubu, sh)."""
    if os.name != "posix":
        return

    def kos(betik, sinir, kontrol=0.1):
        cikti = io.BytesIO()
        t = time.time()
        rc, asildi, son = zamanli_calistir(["sh", "-c", betik], kok, dict(os.environ), sinir, kontrol, cikti)
        return rc, asildi, son, cikti.getvalue(), time.time() - t

    rc, asildi, son, cikti, _ = kos("printf a; sleep 0.3; printf b", 5)
    s.tamam("bekçi: normal süreç çıkış kodu 0, çıktı aynen aktarılır", rc == 0 and not asildi and cikti == b"ab" and son == b"ab")
    rc, asildi, _, _, _ = kos("exit 3", 5)
    s.tamam("bekçi: çıkış kodu aktarılır, zaman aşımı değil", rc == 3 and not asildi)
    rc, asildi, _, cikti, sure = kos("echo basla; sleep 60", 1.0)
    s.tamam("bekçi: sessiz süreç öldürülür ve bildirilir", asildi and rc != 0 and cikti == b"basla\n" and sure < 20)
    rc, asildi, _, cikti, _ = kos("printf x; sleep 0.7; printf y; sleep 0.7; printf z", 1.2)
    s.tamam("bekçi: her çıktı (satır sonsuz da) sayacı sıfırlar", rc == 0 and not asildi and cikti == b"xyz")
    pid_dosyasi = os.path.join(kok, "bekci-pid")
    rc, asildi, _, _, sure = kos('trap "" TERM; sleep 120 & echo $! > %s; echo basla; wait' % pid_dosyasi, 1.0)
    try:
        with open(pid_dosyasi) as f:
            torun = int(f.read().strip())
    except (OSError, ValueError):
        torun = 0
    olu = False
    for _ in range(50):
        try:
            os.kill(torun, 0)
        except OSError:
            olu = True
            break
        time.sleep(0.1)
    s.tamam("bekçi: SIGTERM'i yutan torun da öldürülür (süreç grubu)", asildi and torun > 1 and olu)
    s.tamam("bekçi: SIGTERM'i yutan süreç beklemeden SIGKILL ile sonlandırılır", sure < 30)

    work = os.path.join(kok, "bekci-work")
    env = {"PATH": os.environ["PATH"]}

    def derle_sina(betik, sinir=1.0):
        if os.path.isdir(work):
            _sil(work)
        _yaz(os.path.join(work, "ios.sh"), betik.encode())
        hata = io.StringIO()
        mesaj, sonuc = "", None
        with contextlib.redirect_stderr(hata):
            try:
                sonuc = derle(work, env, ["/bin/sh", "ios.sh"], sinir, lambda k, c, e, sn: zamanli_calistir(k, c, e, sn, 0.1, io.BytesIO()))
            except Kirmizi as e:
                mesaj = str(e)
        return mesaj, hata.getvalue(), sonuc

    mesaj, hata, _ = derle_sina('printf "lame: ok\\n"\necho "INFO: Skipping libass, dependencies built=0, already built=" >> build.log\n'
                                'echo "INFO: Skipping freetype, dependencies built=0, already built=" >> build.log\nsleep 60\n')
    s.tamam("derle: takılan ios.sh kırmızı, son çıktı ve bekleyen kütüphaneler mesajda",
            "hiç çıktı vermedi" in mesaj and "lame: ok" in mesaj and "freetype, libass" in mesaj and "Skipping freetype" in hata)
    # m1 (SK-941): hata mesajı da gizle'den geçer; son çıktıdaki değer mesaja (ve herkese açık adım günlüğüne) girmez.
    # Değer 80 baytı aşar: önce kesip sonra maskelemek ad olmadan kalan değeri sızdırırdı.
    mesaj, hata, _ = derle_sina('printf "lame: ok\\nX_TOKEN=%s\\n"\nsleep 60\n' % ("gizli-deger-77-" * 8))
    s.tamam("derle: takılma mesajındaki son çıktıda gizli değer maskelenir (m1)",
            "hiç çıktı vermedi" in mesaj and "gizli-deger-77" not in mesaj and "X_TOKEN=***" in mesaj)
    mesaj, hata, _ = derle_sina('yes satir | head -n 800000 > build.log\necho GITHUB_TOKEN=cok-gizli >> build.log\necho SON_SATIR >> build.log\nexit 1\n')
    s.tamam("derle: başarısız ios.sh kırmızı; build.log'un yalnız sonu yazdırılır", mesaj == "ios.sh başarısız (kod 1)" and "SON_SATIR" in hata and len(hata) < 4000 * 20)
    s.tamam("derle: yazdırılan build.log sonunda gizli değer maskelenir", "cok-gizli" not in hata and "GITHUB_TOKEN=***" in hata)
    s.tamam("derle: varsayılan sessizlik sınırı SESSIZLIK_SINIRI ve en çok bir saat", derle.__defaults__[2] == SESSIZLIK_SINIRI and 0 < SESSIZLIK_SINIRI <= 3600)
    mesaj, hata, _ = derle_sina('trap "exit 0" TERM\necho basla\nsleep 60 &\nwait\n')
    s.tamam("derle: SIGTERM'e 0 ile çıkan takılı süreç de kırmızı sayılır", "hiç çıktı vermedi" in mesaj)
    mesaj, hata, sonuc = derle_sina('mkdir -p prebuilt/bundle-apple-xcframework-x\n')
    s.tamam("derle: başarılı ios.sh çıktı dizinini döndürür", mesaj == "" and sonuc is not None and sonuc.endswith("bundle-apple-xcframework-x"))
    # SK-947-6: build.log'da Next'in iki indirme satırından biri varsa ios.sh başarılı olsa bile kırmızı
    tamam_sh = "mkdir -p prebuilt/bundle-apple-xcframework-x\n"
    for ad, satir in (("başarılı indirme", "DEBUG: Downloaded https://github.com/x/y to /w/.tmp/y"),
                      ("başarısız indirme", "ERROR: Failed to download https://github.com/x/y to /w/.tmp/y, rc=22. ")):
        mesaj, hata, sonuc = derle_sina('printf "\\n%s\\n" >> build.log\n' % satir + tamam_sh)
        s.tamam("derle: build.log'da '%s' satırı varsa ios.sh başarılı olsa da kırmızı" % ad, "Next indirme satırı" in mesaj and satir[:18].strip() in mesaj and sonuc is None)
    mesaj, hata, sonuc = derle_sina('printf "DEBUG: Downloaded a\\nERROR: Failed to download b\\n" >> build.log\n' + tamam_sh)
    s.tamam("derle: iki indirme satırı birlikte de kırmızı ve ikisi de mesajda", "DEBUG: Downloaded" in mesaj and "ERROR: Failed to download" in mesaj)
    mesaj, hata, sonuc = derle_sina('printf "DEBUG: Downloaded a\\n" >> build.log\nexit 1\n')
    s.tamam("derle: başarısız ios.sh'ta indirme satırı mesaja eklenir", mesaj.startswith("ios.sh başarısız (kod 1); build.log'da Next indirme satırı"))
    mesaj, hata, sonuc = derle_sina('printf "INFO: gnu config already downloaded. Source folder found at /w\\nDEBUG: Downloading custom library x\\n'
                                    'DEBUG: Download skipped\\nERROR: Failed to clone y\\n" >> build.log\n' + tamam_sh)
    s.tamam("derle: indirmeye benzeyen ama Next'in download() satırı olmayan log satırları kırmızı vermez", mesaj == "" and sonuc is not None)
    mesaj, hata, sonuc = derle_sina(tamam_sh)
    s.tamam("derle: build.log hiç yazılmadıysa da (indirme izi yok) yeşil", mesaj == "" and sonuc is not None)


if __name__ == "__main__":
    sys.exit(main())
