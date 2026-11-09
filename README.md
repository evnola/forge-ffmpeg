# forge-ffmpeg

Build recipe, source offer and release assets for the FFmpeg libraries shipped in the **Forge** iOS app (Evnola OÜ):
[FFmpegKitNext](https://github.com/arthenica/ffmpeg-kit-next) `v9.0.2` with FFmpeg `n9.0.2`, built from locked
sources on GitHub-hosted runners, with build provenance attestations.

This repository contains no application code. It holds the recipe (`lock.json`, `build.py`, `patches/`,
`.github/workflows/build.yml`) and publishes the result as GitHub releases.

## Releases

A release is created only by pushing a tag named `v<FFmpeg version>-forge.<N>` (for example `v9.0.2-forge.1`).
Tags are lightweight (no `git tag -a`), so `git ls-remote <repo> refs/tags/<tag>` prints the tagged commit directly.
Manual runs of the workflow (`workflow_dispatch`) are trial builds: they upload a short-lived `deneme-*` artifact and
create neither a release nor an attestation.

Each release contains:

| Asset | Content |
|---|---|
| `<framework>.xcframework.zip` (8 files) | `ffmpegkit`, `libavcodec`, `libavdevice`, `libavfilter`, `libavformat`, `libavutil`, `libswresample`, `libswscale`; slices `ios-arm64` and `ios-arm64-simulator` only |
| `lgpl-manifest.json`, `frameworks.sha256` | SHA-256 of every file inside the unpacked zips, generated from the unpacked archives |
| `source-lock.json` | byte copy of `lock.json` at the tagged commit: every source is pinned by commit |
| `forge-ffmpeg-src-<tag>.tar.xz` | the corresponding source, see below |
| `configure-lines.txt` | the full FFmpeg `configure` line read from the `libavutil` binary of each slice |
| `compiled-set.txt` | the protocols, demuxers, muxers, codecs, parsers, bitstream filters, filters and devices that were compiled in |
| `source-edits.diff` | every change made to the source trees during the build (our patches and the edits Next's scripts make) |
| `toolchain.txt` | Xcode build, tool versions, runner image version, patch results |
| `SHA256SUMS` | SHA-256 of every other asset |

## Corresponding source (source offer)

`forge-ffmpeg-src-<tag>.tar.xz` of a release holds the complete corresponding source of the binaries in that release:

- the 15 source trees at the locked commits, unpatched, exactly as stored by git (`<name>/…`), with `SOURCES.json`
  (name, URL, tag, commit, tree) at the top level. The `dlg` tree is stored inside the FreeType tree, at
  `freetype/subprojects/dlg/…`, where FreeType's own submodule reference points (its record in `SOURCES.json` also has
  `parent` and `path`);
- the recipe that was used, under `forge-ffmpeg/`: `build.py`, `lock.json`, `patches/` and `.github/workflows/build.yml`.

The archive is created before any patch is applied; the patches are applied by `build.py` and are part of the archive,
so the build is reproducible from it. If you cannot obtain the source from the release page, open an issue in this
repository.

Locked sources (the commit of each one is in `lock.json`):

| Source | Version | Upstream mirror |
|---|---|---|
| FFmpegKitNext | 9.0.2 | https://github.com/arthenica/ffmpeg-kit-next |
| FFmpeg | n9.0.2 | https://github.com/arthenica/FFmpeg |
| Expat | R_2_9_0 | https://github.com/arthenica/libexpat |
| fontconfig | 2.18.1 | https://github.com/arthenica/fontconfig |
| FreeType | VER-2-14-3 | https://github.com/arthenica/freetype2 |
| FriBidi | v1.0.16 | https://github.com/arthenica/fribidi |
| HarfBuzz | 14.2.1 | https://github.com/arthenica/harfbuzz |
| LAME | RELEASE__3_100 | https://github.com/arthenica/lame |
| libass | 0.17.4 | https://github.com/arthenica/libass |
| libpng | v1.6.58 | https://github.com/arthenica/libpng |
| libvpx | v1.16.0 | https://github.com/arthenica/libvpx |
| Opus | v1.5.2 | https://github.com/arthenica/opus |
| GNU config scripts (build tool, not linked) | v20210814 | https://github.com/arthenica/gnu-config |
| gas-preprocessor (build tool, not linked; GPL version 2 or later, as stated in the header of `gas-preprocessor.pl`; see below) | v20210917 (commit `d09971fad329d32df19f5bbafe88cf2f0ed04ed7`) | https://github.com/arthenica/gas-preprocessor |
| dlg (FreeType's `subprojects/dlg`, Boost Software License 1.0; build input only, see below) | master, 2025-07-20 | https://github.com/nyorain/dlg (no mirror; a single author's repository) |

Every source is fetched by commit SHA (`git fetch --depth 1 <url> <commit>`) and the build stops if the SHA cannot be
fetched; it never falls back to a tag or a branch. The commit of each tree is checked against `lock.json` before the
build and again after it, and any directory under `src/` or `.tmp/source/` that is not in the lock fails the build.

`gas-preprocessor.pl` is a Perl script that FFmpegKitNext hands to every arm64 library as its assembler (`AS`), so all aarch64
assembly of the FFmpeg libraries is pre-processed by it. It is a build tool: it does not end up in the frameworks, and the
licence line in its header (`This code is licensed under GPLv2 or later`) is the only licence statement the upstream tree has.
Next downloads it from a movable tag URL when it is missing; `build.py` therefore locks it like the other sources and the
build never downloads it:

- `lock.json` has the commit and, only for this source, a `sha256` key with the SHA-256 of the blob `gas-preprocessor.pl` at
  that commit (the lock is by commit and hash; the tag in `ref` is only checked to point at the commit). The file is read
  from the fetched git object, its hash is compared with the lock, and the build stops if they differ.
- Before `ios.sh` starts, `build.py` writes `work/.tmp/gas-preprocessor.pl` (mode 0755, a new regular file; an existing path
  or a link stops the build). The bytes are the locked blob after the same two string replacements that Next's own `sed`
  makes (`s+({` to `s+(\{` and `s*})` to `s*\})`, each exactly once; they stop an unescaped brace error of newer Perl), and
  their SHA-256 is a constant in `build.py` that is checked at placement time. Next then finds the file and skips its
  download branch (see patch 08 below).
- `verify-lock` compares the placed file with that SHA-256 before and after the build, and before the build it also requires
  the top level of `.tmp/` to be exactly `source` and `gas-preprocessor.pl`.
- The Perl that runs the script is the first `perl` on `PATH`; `toolchain.txt` records its path and version.

No locked tree may contain a git submodule reference (gitlink) or a `.gitmodules` file, with two exceptions that are a
fixed table in `build.py` (not read from `lock.json` or from arguments):

- FreeType's tree has exactly one gitlink, `subprojects/dlg`, and it must point to the commit that `lock.json` locks for
  `dlg`. Any other path, a second gitlink, the same gitlink in another tree, or a different commit fails the build.
  `build.py` fetches `dlg` by that commit into `src/freetype/subprojects/dlg` after FreeType, and the source archive
  stores it there.
- A `.gitmodules` file is allowed only at the root of a tree, and only as one of two exact blobs: the empty file (Opus
  has one; allowed only in a tree without gitlinks) or FreeType's own (`[submodule "dlg"]`, allowed only in the FreeType
  tree together with the gitlink above).

`dlg` is a build input, not part of the binaries: FreeType's `make` needs its headers, but `FT_DEBUG_LOGGING` is off in
`ftoption.h`, so `dlgwrap.c` compiles to an empty unit and no dlg code reaches the frameworks. The `check` stage measures
this (see below) instead of assuming it. Because the Boost license asks for no notice in object code, the app's license
list does not change; the dlg `LICENSE` is in the source archive.

## FFmpeg signed tag

The locked FFmpeg commit `946fcce07b6dcd0331c8cc609192aeff5e1924f8` is the commit of the upstream release tag `n9.0.2`
of `FFmpeg/FFmpeg`. That tag is an annotated tag object (`ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3`) with an OpenPGP
signature by Michael Niedermayer (issuer fingerprint `DD1E C9E8 DE08 5C62 9B3E  1846 B18E 8928 B394 8D64`). The build
fetches the commit from the `arthenica/FFmpeg` mirror by SHA, so the mirror is only a transport: what counts is that the
SHA in `lock.json` equals the commit of the upstream signed tag.

Checked once, on 2026-11-08, when the lock was written:

- GitHub API (`gh api repos/FFmpeg/FFmpeg/git/tags/ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3`): `verification.verified`
  is `true`, `reason` is `valid`, `verified_at` is `2026-09-18T13:12:36Z`. The tag points to the locked commit.
- Locally, `git verify-tag n9.0.2` in a new, empty keyring with the key fetched from `keys.openpgp.org` by the
  fingerprint above printed `Good signature from "Michael Niedermayer <michael-git@niedermayer.cc>"`, followed by the
  usual warning that the key is not certified by a trusted signature. So the signature is cryptographically valid for
  that key. That the key belongs to the FFmpeg maintainer rests on GitHub's record of the signer's account key and on
  the fingerprint being the one the tag shows; we did not establish it through a web of trust or a statement signed by
  the FFmpeg project.
- The "FFmpeg release signing key" that ffmpeg.org publishes (`FCF9 86EA 15E6 E293 A564  4F10 B432 2F04 D676 58D8`) is
  **not** the key of this tag: with only that key imported, `git verify-tag` prints "No public key". It signs release
  tarballs, not git tags.

To check it again (the history is not needed):

```
git init ff && cd ff
git fetch --depth 1 https://github.com/FFmpeg/FFmpeg tag n9.0.2
git rev-parse n9.0.2 "n9.0.2^{commit}"     # ce8f11b9fac4cd7ccfb41ca7c01bc17f5cda8cc3, then 946fcce07b6dcd0331c8cc609192aeff5e1924f8
git verify-tag n9.0.2                      # prints the issuer fingerprint; "No public key" until the key is imported
gpg --fetch-keys "https://keys.openpgp.org/vks/v1/by-fingerprint/DD1EC9E8DE085C629B3E1846B18E8928B3948D64"
git verify-tag n9.0.2                      # the primary key fingerprint printed must be the one above
```

In a checkout of this repository, `grep -A5 '"name": "ffmpeg"' lock.json` must show the commit
`946fcce07b6dcd0331c8cc609192aeff5e1924f8`.

## Verify a release

```
gh release download <tag> -R evnola/forge-ffmpeg -D dist
git ls-remote https://github.com/evnola/forge-ffmpeg "refs/tags/<tag>"      # tag commit
( cd dist && sha256sum -c SHA256SUMS )
gh attestation verify dist/<asset> -R evnola/forge-ffmpeg \
  --cert-identity "https://github.com/evnola/forge-ffmpeg/.github/workflows/build.yml@refs/tags/<tag>" \
  --source-ref "refs/tags/<tag>" --source-digest <tag commit> \
  --deny-self-hosted-runners --format json
```

The attestation is created by the `release` job from the files the `build` job produced; the `release` job recomputes
every checksum and compares it with `SHA256SUMS` and with the digest the `build` job reported. All jobs run on
GitHub-hosted runners (`macos-26`, `ubuntu-24.04`).

## What is built

- FFmpegKitNext is built with `ios.sh --xcframework --target=16.1 --no-bitcode --no-ffmpeg-kit-protocols
  --arch=arm64,arm64-simulator --reconf-lib-lame --disable-lib-libjxl`, with the system libraries AudioToolbox,
  AVFoundation, VideoToolbox, zlib, bzip2 and libiconv, and the external libraries libvpx, Opus, libass, fontconfig,
  FreeType, FriBidi and LAME (HarfBuzz, Expat and libpng are their dependencies). The result is built as dynamic
  frameworks. Next's build scripts also enable libjxl whenever FreeType is enabled; `--disable-lib-libjxl` switches that
  off, and the `check` stage fails the build if the compiled FFmpeg has any `--enable-lib*` option beyond the eight
  above (libvpx, libopus, libass, libfontconfig, libfreetype, libfribidi, libharfbuzz, libmp3lame).
- FreeType is built without brotli (`patches/05-freetype-brotli-off.json`): brotli is not a locked source, and without
  it the WOFF2 decoding path, which would handle fonts from untrusted subtitle files, is closed. The `check` stage fails
  if any binary links to brotli (`otool -L`) or contains a brotli symbol or string. The build environment has no
  `PKG_CONFIG_PATH` (it is an allow-list of variables), so Homebrew's `.pc` files cannot add libraries to the build.
  Absence is also measured positively (`patches/06-freetype-pc-no-brotli.json` removes the stale `libbrotlidec`
  requirement from `freetype2.pc`): the `check` stage fails unless every installed `prebuilt/*/pkgconfig/freetype2.pc`
  has exactly one `Requires.private` line and it is `Requires.private: zlib`, no `.pc` file under `prebuilt/` mentions
  brotli, and the installed `prebuilt/*/freetype/include/freetype2/freetype/config/ftoption.h` (the configure output, not
  the copy in the source tree) has no active `#define FT_CONFIG_OPTION_USE_BROTLI`. A missing file fails the stage. The
  measurement, with the SHA-256 of each file, is a line in `configure-lines.txt`.
- FFmpeg is configured with `--enable-version3` and without `--enable-gpl` and `--enable-nonfree`: the binaries are
  **LGPL version 3 or later**. No GPL library is linked.
- Five configure flags are added by `patches/01-configure-security-flags.json`, in this order:
  `--disable-network --disable-protocols --enable-protocol=file --disable-demuxer=hls,dash,concat,image2 --disable-muxer=tee`.
  The `check` stage of `build.py` fails the build unless the compiled-in protocol set is exactly `{file}`, none of the
  `hls`, `dash`, `concat`, `image2` demuxers and not the `tee` muxer is compiled in, every binary links only
  `/usr/lib/`, `/System/Library/` and `@rpath/` paths, and the configure line of both slices passes the flag rules.
- `strip-frameworks.sh` and `SOURCE`, which Next places in the framework roots, are removed before packaging.

## Changes made to third-party source

All changes are applied by `build.py` (the patch files carry the date and the notice) or by FFmpegKitNext's own scripts.
`source-edits.diff` in each release is the machine-generated record. Modified by Evnola OÜ, 2026-11-08:

- `patches/01-configure-security-flags.json`: inserts the five configure flags above into `scripts/apple/ffmpeg.sh`
  of FFmpegKitNext, immediately before `${CONFIGURE_POSTFIX}`.
- `patches/02-expat-r2-9-0.json`: changes the Expat tag in `scripts/source.sh` of FFmpegKitNext from `R_2_8_1` to
  `R_2_9_0`. The Expat source in the lock is `R_2_9_0`; Next reuses a non-empty `src/expat`, so this only keeps the
  recipe consistent.
- `patches/03-harfbuzz-wrap-nodownload.json`: inserts `--wrap-mode=nodownload` into the `meson setup` call of
  `scripts/apple/harfbuzz.sh` of FFmpegKitNext, immediately before `--buildtype=release`. HarfBuzz ships
  `subprojects/*.wrap` files; without the option meson may download a missing dependency during the build, with it
  meson fails instead.
- `patches/04-freetype-autogen-no-submodule-fetch.json`: in `autogen.sh` of FreeType, replaces the line
  `git submodule update --init` by an error message and `exit 1`. That branch only runs when `subprojects/dlg` is missing;
  `build.py` places the locked dlg there before the build, so it is dead code, and if the layout ever breaks the build
  stops instead of downloading anything.
- `patches/05-freetype-brotli-off.json`: changes `--with-brotli=yes` to `--with-brotli=no` in `scripts/apple/freetype.sh`
  of FFmpegKitNext. Brotli is not a locked source (see above).
- `patches/06-freetype-pc-no-brotli.json`: in `create_freetype_package_config` of `scripts/function-apple.sh` of
  FFmpegKitNext, removes `libbrotlidec` from the `Requires.private` line of the `freetype2.pc` that Next writes by hand
  (`Requires.private: zlib, libbrotlidec` becomes `Requires.private: zlib`). FreeType is built without brotli (patch 05),
  so no `libbrotlidec.pc` exists, and the unpatched line would stop every pkg-config user of `freetype2` (fontconfig,
  libass, FFmpeg).
- `patches/07-freetype-no-libjxl-wait.json`: in the library build loop of `scripts/main-ios.sh` of FFmpegKitNext, the
  `freetype` case no longer waits for libjxl (`[[ $OK_libpng -eq 1 ]] && [[ $OK_libjxl -eq 1 ]]` becomes
  `[[ $OK_libpng -eq 1 ]]`). libjxl is disabled by `--disable-lib-libjxl`, so it is never built and `OK_libjxl` is never
  set; without this patch the loop would wait for it forever. FreeType does not use libjxl.
- `patches/08-gas-preprocessor-no-download.json`: in `set_toolchain_paths` of `scripts/function-ios.sh` of FFmpegKitNext,
  the branch that downloads `gas-preprocessor.pl`, makes it executable and patches it with `sed` is replaced by a log line
  and an unconditional `exit 1`. `build.py` places the locked file before `ios.sh`, so the branch is dead code; if the file
  is ever missing, the build stops instead of downloading. (Next's own error path runs `exit 1` inside a command
  substitution, which does not stop the build.) `scripts/main-ios.sh` also calls this function, for FFmpeg; the `exit 1`
  inside the function covers that call too.
- `patches/09-run-apple-toolchain-exit.json`: in `scripts/run-apple.sh` of FFmpegKitNext, `set_toolchain_paths "${LIB_NAME}"`
  becomes `set_toolchain_paths "${LIB_NAME}" || exit 1`. The script ignored the return code, so a library could be
  configured with an empty toolchain and an empty pkg-config install directory.
- libpng: no libpng patch is applied because `pngpriv.h` of the locked libpng does not contain the `TARGET_OS_MAC`
  condition. `build.py` measures this in the `patch` stage (the build fails if the condition appears) and records the
  result in `toolchain.txt`.

FFmpegKitNext's `scripts/apple/ffmpeg.sh` also edits the FFmpeg tree during every build (unchanged by us):

- `configure`: `s/check_cflags -mdynamic-no-pic && add_asflags -mdynamic-no-pic;/check_cflags -mdynamic-no-pic;/g`
- `libavcodec/videotoolbox.c`: the `kCVPixelBufferOpenGLESCompatibilityKey` line is commented out for Mac Catalyst and
  visionOS and restored otherwise (iOS: restored).
- `libavutil/log.c`: `s/static atomic_int av_log_level/__thread atomic_int av_log_level/g`

## Reproduce a build

You need an arm64 Mac with macOS 26, Xcode 26.6 (build `17F113`) at `/Applications/Xcode_26.6.app`, Python 3 and the
Homebrew packages `autoconf automake libtool gettext meson nasm yasm gperf bison ragel texinfo gtk-doc gnu-sed xz`,
plus `cmake`, `ninja` and `pkgconf`.

```
python3 build.py selftest
python3 build.py all --work /new/work-dir --out /new/dist-dir --label deneme-0000000
```

`build.py all` fetches the locked commits with `git fetch` and starts Next's `ios.sh` with a clean environment (no
inherited tokens or other variables).

Next's own scripts and the locked sources can still try to download things while they build, and the build is not run
without network access. What stops or detects this:

- HarfBuzz: `meson setup` runs with `--wrap-mode=nodownload` (`patches/03-harfbuzz-wrap-nodownload.json`), so a missing
  dependency fails the build instead of being fetched from a wrap.
- FreeType: its `autogen.sh` would run `git submodule update --init` if `subprojects/dlg` were missing, which would clone
  a dependency from the network. `patches/04-freetype-autogen-no-submodule-fetch.json` turns that line into an error.
  The locked dlg is in place before the build, so the line is not reached.
- After the build, `verify-lock --asama sonra` fails if a locked tree contains a `.git` entry other than its own root
  `.git` (the one exception is `src/freetype/subprojects/dlg/.git`, matched by exact path), a `.git/modules` directory,
  or a `subprojects/<x>/` directory that is not a directory of the locked commit (this also catches meson's
  `packagecache/` and `<name>-<version>/` directories), and if a directory under `src/` or `.tmp/source/` is not in the
  lock. The nested dlg repository must still be at the locked commit with a clean `git status`.
- The `check` stage measures that `FT_DEBUG_LOGGING` is not defined: `ftoption.h` has no active `#define`, and the macro
  does not occur in the build environment, in the FFmpeg configure lines or in Next's `scripts/`. The measurement (with
  the SHA-256 of `ftoption.h`) is a line in `configure-lines.txt`.
- Expected noise in `build.log`: FreeType's `freetype.sh` still asks pkg-config for `libbrotlidec` (`BROTLI_CFLAGS`,
  `BROTLI_LIBS`) and the log therefore contains "Package libbrotlidec was not found" on every build. With patch 05 those
  variables are not used, and the lines are deliberately not patched. Nothing that reads `build.log` may key on
  "not found" or "libbrotlidec"; the brotli checks above are the evidence.

- `gas-preprocessor.pl` is locked, placed before the build and checked in both `verify-lock` stages, and patches 08 and 09
  stop the build instead of downloading it (see above).
- `derle` fails the build, even if `ios.sh` succeeded, when `build.log` contains one of the two lines that Next's `download()`
  writes: `DEBUG: Downloaded ` or `ERROR: Failed to download`. This build is not expected to download anything. (The
  `clone_git_repository_with_*` functions write other lines; their targets are covered by `verify-lock`.)

Not covered: a download that leaves none of these traces (for example a file fetched into a temporary directory or into
a path that already exists in the tree).

### Failed or cancelled runs

- `build.py build` kills the whole Next process group if `ios.sh` prints nothing for 40 minutes (`SESSIZLIK_SINIRI`) and
  fails with the last output and the libraries that `build.log` shows as waiting for a dependency. Next prints
  `<library>: ` when a library starts and `ok` when it ends, so silence means that no library (or the scheduler
  between libraries) makes progress.
- A failed `git fetch` of a locked commit or tag reports the last lines of git's own error message, with URL
  credentials and token-shaped strings masked.
- When the `build` job fails or is cancelled, `python3 build.py tani` collects the head and tail of `build.log` (sizes
  capped, the same masking applied), the `config.log`, `meson-log.txt` and CMake logs under `src/`, and a listing of
  `prebuilt/`. The workflow uploads them as the artifact `tani-<run id>` with a 3-day retention; they are never part of
  a release. The build runs in an allow-listed environment, so the logs are not expected to contain secrets; the masking
  is a second line of defence, not a guarantee.

## License

`LGPL-3.0-or-later`, see [LICENSE](LICENSE). The LGPL version 3 is written as additional permissions on top of the GNU
GPL version 3, so [COPYING](COPYING) holds the full, unmodified text of the GPL version 3 that it refers to. Files under
`patches/` carry the license of the project they patch, see `LICENSE`. The third-party sources and the
binaries built from them are under their own licenses; they are in the source trees of the source archive and in the
`LICENSE*` files inside the frameworks.
