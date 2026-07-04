#!/usr/bin/env bash
set -euo pipefail

TEMP_DIR="${TEMP_DIR:-.work/resolver}"
BIN_DIR="${BIN_DIR:-bin}"
APKSIGNER="${APKSIGNER:-}"
HTMLQ="${HTMLQ:-htmlq}"
apkmirror_example_url="${apkmirror_example_url:-}"
__AAV__="${__AAV__:-false}"
mkdir -p "$TEMP_DIR"

pr() { echo >&2 -e "[+] ${1}"; }
epr() { echo >&2 -e "[-] ${1}"; }
wpr() { echo >&2 -e "[!] ${1}"; }
isoneof() { local i=$1; shift; for v; do [ "$v" = "$i" ] && return 0; done; return 1; }
req() { local url=$1 output=$2; if [ "$output" = - ]; then curl -L --fail -sS "$url"; else curl -L --fail -sS "$url" -o "$output"; fi; }
gh_req() { local url=$1 output=$2; local headers=(); [ -n "${GITHUB_TOKEN-}" ] && headers=(-H "Authorization: token ${GITHUB_TOKEN}"); if [ "$output" = - ]; then curl -L --fail -sS "${headers[@]}" -H 'Accept: application/vnd.github+json' "$url"; else curl -L --fail -sS "${headers[@]}" -H 'Accept: application/vnd.github+json' "$url" -o "$output"; fi; }
gh_dl() { req "$2" "$1"; }

ensure_htmlq() {
  if command -v "$HTMLQ" >/dev/null 2>&1; then return 0; fi
  echo "htmlq is required. Install it or set HTMLQ to an htmlq binary." >&2
  return 1
}

merge_splits() {
  local bundle=$1 output=$2
  if unzip -l "$bundle" 2>/dev/null | grep -q '^[[:space:]]*[0-9].*AndroidManifest\.xml$'; then
    mv -f "$bundle" "$output"
    return 0
  fi
  gh_dl "$TEMP_DIR/apkeditor.jar" "https://github.com/REAndroid/APKEditor/releases/download/V1.4.9/APKEditor-1.4.9.jar" >/dev/null || return 1
  java -jar "$TEMP_DIR/apkeditor.jar" merge -i "$bundle" -o "${output}-unsigned" -clean-meta -f >/dev/null
  mv -f "${output}-unsigned" "$output"
}

_fs_get() {
  local url=$1 referer=${2:-}
  local fs_url="${FLARESOLVERR_URL:-}"

  if [ -n "$fs_url" ] && command -v jq >/dev/null 2>&1; then
    local extra_headers="" response status
    [ -n "$referer" ] && extra_headers=",\"headers\":{\"Referer\":\"$referer\"}"
    response=$(curl -sS -X POST "${fs_url%/}/v1" \
      -H 'Content-Type: application/json' \
      -d "{\"cmd\":\"request.get\",\"url\":\"$url\",\"maxTimeout\":60000${extra_headers}}") || true
    status=$(echo "$response" | jq -r '.status // empty' 2>/dev/null || true)
    if [ "$status" = ok ]; then
      html=$(echo "$response" | jq -r '.solution.response // empty')
      FS_COOKIES=$(echo "$response" | jq -r '[.solution.cookies[]? | .name + "=" + .value] | join("; ")')
      user_agent=$(echo "$response" | jq -r '.solution.userAgent // "Mozilla/5.0"')
      export FS_COOKIES
      return 0
    fi
    wpr "FlareSolverr failed for $url, falling back to curl"
  fi

  local headers=(-H "User-Agent: ${user_agent:-Mozilla/5.0}")
  [ -n "$referer" ] && headers+=(-H "Referer: $referer")
  html=$(curl -L --fail -sS "${headers[@]}" "$url") || return 1
  user_agent="${user_agent:-Mozilla/5.0}"
  FS_COOKIES=""
}
# -------------------- apkmirror --------------------
get_apkmirror_resp() {
	local html=""
	_fs_get "${1}" || return 1
	__APKMIRROR_RESP__="$html"
	__APKMIRROR_CAT__="${1##*/}"
	__APKMIRROR_EXAMPLE_URL__="${apkmirror_example_url:-}"
}

get_apkmirror_vers() {
	local vers apkm_resp html=""
	_fs_get "https://www.apkmirror.com/uploads/?appcategory=${__APKMIRROR_CAT__}" || return 1
	apkm_resp="$html"
	vers=$(sed -n 's;.*Version:</span><span class="infoSlide-value">\(.*\) </span>.*;\1;p' <<<"$apkm_resp" | awk '{$1=$1}1')
	if [ "$__AAV__" = false ]; then
		local IFS=$'\n'
		vers=$(grep -iv "\(beta\|alpha\)" <<<"$vers")
		local v r_vers=()
		for v in $vers; do
			grep -iq "${v} \(beta\|alpha\)" <<<"$apkm_resp" || r_vers+=("$v")
		done
		echo "${r_vers[*]}"
	else
		echo "$vers"
	fi
}

get_apkmirror_pkg_name() { sed -n 's;.*id=\(.*\)" class="accent_color.*;\1;p' <<<"$__APKMIRROR_RESP__"; }

apkmirror_search() {
	local resp="$1" dpi="$2" arch="$3" apk_bundle="$4"
	local dlurl="" node app_table emptyCheck

	local apparch=('universal' 'noarch' 'arm64-v8a + armeabi-v7a')
	if [ "$arch" != all ]; then
		apparch+=("$arch")
	fi

	local appdpi=("nodpi" "anydpi")
	local match_any_dpi=false
	if [ "$dpi" ]; then
		appdpi+=($dpi)
		if isoneof "auto" "${appdpi[@]}"; then
			match_any_dpi=true
		fi
	fi

	local best_fallback_url=""

	for ((n = 1; n < 40; n++)); do
		node=$($HTMLQ "div.table-row.headerFont:nth-last-child($n)" <<<"$resp")
		if [ -z "$node" ]; then break; fi
		
		dlurl=$($HTMLQ --base https://www.apkmirror.com --attribute href "div.table-cell:nth-child(1) > a:nth-child(1)" <<<"$node")
		if [ -z "$dlurl" ]; then break; fi

		local node_apk_bundle node_arch node_dpi
		node_apk_bundle=$($HTMLQ "div.table-cell:nth-child(1) span.apkm-badge:first-of-type" --text <<<"$node" | xargs)
		[ -z "$node_apk_bundle" ] && node_apk_bundle="APK"

		node_arch=$($HTMLQ "div.table-cell:nth-child(2)" --text <<<"$node" | xargs)
		node_dpi=$($HTMLQ "div.table-cell:nth-child(4)" --text <<<"$node" | xargs)

		if [ "$node_apk_bundle" != "$apk_bundle" ]; then continue; fi

		if isoneof "$node_arch" "${apparch[@]}"; then
			if isoneof "$node_dpi" "${appdpi[@]}"; then
				echo "$dlurl"
				return 0
			elif [ "$match_any_dpi" = true ] && [ -z "$best_fallback_url" ]; then
				best_fallback_url="$dlurl"
			fi
		fi
	done

	if [ -n "$best_fallback_url" ]; then
		echo "$best_fallback_url"
		return 0
	fi
	return 1
}

dl_apkmirror() {
	local url=$1 version=${2// /-} output=$3 arch=$4 dpi=$5 is_bundle=false
	local base_url="https://www.apkmirror.com"
	local html=""

	if [ -f "${output}.apkm" ]; then
		merge_splits "${output}.apkm" "${output}"
		return 0
	fi

	if [ "$arch" = "arm-v7a" ]; then arch="armeabi-v7a"; fi

	local resp release_url=""

	if [ -n "${__APKMIRROR_EXAMPLE_URL__:-}" ]; then
		local example_path="${__APKMIRROR_EXAMPLE_URL__#$base_url}"
		local slug_ver target_ver
		slug_ver=$(echo "$example_path" | grep -oP '\d+(-\d+)+' | tail -1)
		target_ver=$(echo "$version" | tr '.' '-' | grep -oP '\d+(-\d+)+')
		if [ -n "$slug_ver" ] && [ -n "$target_ver" ]; then
			release_url="${base_url}${example_path/$slug_ver/$target_ver}"
				_fs_get "$release_url" || true
			resp="$html"
			if [[ "$resp" == *"Page Not Found"* ]] || [[ "$resp" == *"404 Whoops"* ]] || [ -z "$resp" ]; then
					release_url=""
			fi
		fi
	fi

	local search_version="${version//./-}"
	search_version="${search_version//_/-}"
	search_version="${search_version,,}"
	search_version="${search_version//[^a-z0-9-]/}"
	search_version="${search_version//---/-}"

	if [ -z "$release_url" ]; then
		local apkmname
		apkmname=$($HTMLQ "h1.marginZero" --text <<<"$__APKMIRROR_RESP__")
		apkmname="${apkmname,,}" apkmname="${apkmname// /-}" apkmname="${apkmname//[^a-z0-9-]/}"
		release_url="${url%/}/${apkmname}-${search_version}-release/"
		_fs_get "$release_url" || true
		resp="$html"
		if [[ "$resp" == *"Page Not Found"* ]] || [[ "$resp" == *"404 Whoops"* ]] || [ -z "$resp" ]; then
			release_url=""
		fi
	fi

	if [ -z "$release_url" ]; then
		local list_url="https://www.apkmirror.com/uploads/?appcategory=${__APKMIRROR_CAT__}"
		local version_href=""
		for page_num in $(seq 1 5); do
			local page_url="$list_url"
			[[ $page_num -gt 1 ]] && page_url="${list_url%%\?*}/page/$page_num/?${list_url#*\?}"
			_fs_get "$page_url" || return 1
			version_href=$(echo "$html" | grep -oP 'href="\K/apk/[^"]*'"$search_version"'[^"]*release[^"]*' | head -1) || true
			if [ -n "$version_href" ]; then
				release_url="$base_url$version_href"
				_fs_get "$release_url" || return 1
				resp="$html"
				break
			fi
		done
		if [ -z "$release_url" ]; then
			epr "Could not find version $version on APKMirror"
			return 1
		fi
	fi

	local node dlurl=""
	node=$($HTMLQ "div.table-row.headerFont:nth-last-child(1)" -r "span:nth-child(n+3)" <<<"$resp")
	if [ "$node" ]; then
		for type in APK BUNDLE; do
			if dlurl=$(apkmirror_search "$resp" "$dpi" "$arch" "$type"); then
				[ "$type" = "BUNDLE" ] && is_bundle=true || is_bundle=false
				break
			fi
		done
		if [ -z "$dlurl" ]; then return 1; fi
		_fs_get "$dlurl" || return 1
		resp="$html"
	fi

	local all_dl_btns btn_url
	all_dl_btns=$(echo "$resp" | $HTMLQ "a.downloadButton" --attribute href)
	if [ "$is_bundle" = true ]; then
		btn_url=$(echo "$all_dl_btns" | grep -v 'forcebaseapk' | head -1)
		[ -z "$btn_url" ] && btn_url=$(echo "$all_dl_btns" | head -1)
	else
		btn_url=$(echo "$all_dl_btns" | grep 'forcebaseapk' | head -1)
		[ -z "$btn_url" ] && btn_url=$(echo "$all_dl_btns" | head -1)
	fi
	if [ -z "$btn_url" ]; then epr "Could not find download button on APKMirror"; return 1; fi
	btn_url=$(echo "$btn_url" | sed 's/&amp;/\&/g')

	_fs_get "$base_url$btn_url" || return 1
	local final_url
	final_url=$($HTMLQ "a#download-link" --attribute href <<<"$html" 2>/dev/null | head -1) || true
	[ -z "$final_url" ] && final_url=$(echo "$html" | grep -oP 'id="download-link"[^>]*href="\K[^"]+' | head -1) || true
	if [ -z "$final_url" ]; then epr "Could not find final download link on APKMirror"; return 1; fi
	final_url=$(echo "$final_url" | sed 's/&amp;/\&/g')
	[[ "$final_url" != http* ]] && final_url="${base_url}${final_url}"

	pr "Downloading APK: $final_url"
	local cookie_args=()
	[ -n "${FS_COOKIES:-}" ] && cookie_args=(--header "Cookie: $FS_COOKIES")
	local referer_url="$base_url$btn_url"
	[[ "$btn_url" == http* ]] && referer_url="$btn_url"

	if [ "$is_bundle" = true ]; then
		wget -nv -O "${output}.apkm" \
			--header="User-Agent: ${user_agent:-Mozilla/5.0}" \
			--referer="$referer_url" \
			"${cookie_args[@]}" \
			--timeout=300 \
			"$final_url" || return 1
		if ! unzip -t "${output}.apkm" >/dev/null 2>&1; then
			epr "Downloaded file is not a valid zip (apkm): $final_url"
			return 1
		fi
		merge_splits "${output}.apkm" "${output}"
	else
		wget -nv -O "${output}" \
			--header="User-Agent: ${user_agent:-Mozilla/5.0}" \
			--referer="$referer_url" \
			"${cookie_args[@]}" \
			--timeout=300 \
			"$final_url" || return 1
	fi
}

# -------------------- apkpure --------------------
get_apkpure_resp() {
	local url=$1
	url="${url%/downloading*}"
	url="${url%/}"
	__APKPURE_BASE_URL__="$url"
	__APKPURE_PKG__=$(echo "$url" | grep -oP '[a-zA-Z][a-zA-Z0-9]*(\.[a-zA-Z][a-zA-Z0-9]*){1,}' | tail -1)
	local html=""
	_fs_get "${url}/downloading/" || return 1
	__APKPURE_RESP__="$html"
}

get_apkpure_vers() {
	local ver
	ver=$(echo "$__APKPURE_RESP__" | sed 's/<h2[^>]*>/\n__H2__/g' | grep '__H2__' | sed 's/__H2__//' | grep -oP '[0-9]+\.[0-9][0-9.]*' | head -1) || true
	[ -z "$ver" ] && ver=$(echo "$__APKPURE_RESP__" | grep -oP '"softwareVersion":"\K[^"]+' | head -1) || true
	echo "$ver"
}

get_apkpure_pkg_name() { echo "$__APKPURE_PKG__"; }

dl_apkpure() {
	local url=$1 version=$2 output=$3 arch=${4:-} _dpi=${5:-}
	local html=""

	local dl_page_url
	if [ -n "$version" ]; then
		dl_page_url="${__APKPURE_BASE_URL__}/downloading/${version}"
	else
		dl_page_url="${__APKPURE_BASE_URL__}/downloading"
	fi

	_fs_get "$dl_page_url" || return 1

	if [ -z "$version" ]; then
		version=$(echo "$html" | sed 's/<h2[^>]*>/\n__H2__/g' | grep '__H2__' | sed 's/__H2__//' | grep -oP '[0-9]+\.[0-9][0-9.]*' | head -1) || true
		[ -z "$version" ] && version=$(echo "$html" | grep -oP '"softwareVersion":"\K[^"]+' | head -1) || true
	fi

	local download_url
	download_url=$($HTMLQ "a#download_link" --attribute href <<<"$html" 2>/dev/null | head -1) || true
	[ -z "$download_url" ] && \
		download_url=$(echo "$html" | grep -oP '<a[^>]+id="download_link"[^>]+href="\Khttps://[^"]+' | head -1) || true
	[ -z "$download_url" ] && \
		download_url=$(echo "$html" | grep -oP 'id="download_link"[^>]*href="\Khttps://[^"]+' | head -1) || true

	if [ -z "$download_url" ]; then
		epr "Could not find download link on APKPure"
		return 1
	fi

	pr "Downloading from APKPure: $download_url"
	local cookie_header=()
	[ -n "${FS_COOKIES:-}" ] && cookie_header=(-H "Cookie: $FS_COOKIES")

	local is_bundle=false
	echo "$download_url" | grep -qi 'xapk' && is_bundle=true

	if [ "$is_bundle" = true ]; then
		curl -L -s -S \
			-H "User-Agent: ${user_agent:-Mozilla/5.0}" \
			-H "Referer: $dl_page_url" \
			"${cookie_header[@]}" \
			--connect-timeout 30 --max-time 300 \
			"$download_url" -o "${output}.xapk" || return 1
		_apkpure_install_xapk "${output}.xapk" "${output}" || return 1
	else
		curl -L --fail -s -S \
			-H "User-Agent: ${user_agent:-Mozilla/5.0}" \
			-H "Referer: $dl_page_url" \
			"${cookie_header[@]}" \
			--connect-timeout 30 --max-time 300 \
			"$download_url" -o "${output}" || return 1
	fi
}

_apkpure_install_xapk() {
	local xapk=$1 output=$2
	if ! unzip -t "$xapk" >/dev/null 2>&1; then
		epr "Downloaded XAPK is not a valid zip (Cloudflare block?): $xapk"
		return 1
	fi
	gh_dl "$TEMP_DIR/apkeditor.jar" "https://github.com/REAndroid/APKEditor/releases/download/V1.4.9/APKEditor-1.4.9.jar" >/dev/null || return 1
	if unzip -l "$xapk" 2>/dev/null | grep -q '^[[:space:]]*[0-9].*base\.apk$'; then
		pr "Extracting base.apk from XAPK"
		unzip -p "$xapk" base.apk > "$output" || return 1
	else
		pr "Merging XAPK splits with APKEditor"
		local OP
		if ! OP=$(java -jar "$TEMP_DIR/apkeditor.jar" m -i "$xapk" -o "${output}-unsigned" 2>&1); then
			epr "APKEditor m error: $OP"
			return 1
		fi
		if ! OP=$(java -jar "$APKSIGNER" sign --ks ks-p12.keystore --ks-pass pass:123456789 --key-pass pass:123456789 --ks-key-alias jhc \
			--out "$output" "${output}-unsigned" 2>&1); then
			epr "apksigner error: $OP"
			return 1
		fi
		rm "${output}.idsig" "${output}-unsigned" 2>/dev/null || :
	fi
}

# -------------------- apkcombo --------------------
get_apkcombo_resp() {
	local url=$1
	url="${url%/}"
	__APKCOMBO_PKG__="${url##*/}"
	__APKCOMBO_BASE_URL__="$url"
	local html=""
	_fs_get "https://apkcombo.com/search/${__APKCOMBO_PKG__}/download" ||
		_fs_get "${__APKCOMBO_BASE_URL__}/download" ||
		_fs_get "${__APKCOMBO_BASE_URL__}" ||
		return 1
	__APKCOMBO_RESP__="$html"
}
get_apkcombo_vers() {
	{
		echo "$__APKCOMBO_RESP__" | grep -oP 'phone-\K[0-9][^-]+-apk' | sed 's/-apk$//'
		echo "$__APKCOMBO_RESP__" | grep -oP '"softwareVersion"\s*:\s*"\K[^"]+'
		echo "$__APKCOMBO_RESP__" | grep -oP 'Version</[^>]+>\s*<[^>]+>\K[^<]+' || true
	} | sed '/^$/d' | head -1
}
get_apkcombo_pkg_name() { echo "$__APKCOMBO_PKG__"; }
dl_apkcombo() {
	local _url=$1 version=$2 output=$3 _arch=$4 _dpi=$5
	local html="" dl_url final_url checkin page_url page compact_page

	if [ -n "$version" ]; then
		local sfxs=("apk" "xapk" "apks")
	else
		local sfxs=("apk")
	fi

	for sfx in "${sfxs[@]}"; do
		if [ -n "$version" ]; then
			page_url="https://apkcombo.com/search/${__APKCOMBO_PKG__}/download/phone-${version}-${sfx}"
		else
			page_url="https://apkcombo.com/search/${__APKCOMBO_PKG__}/download/apk"
		fi

		_fs_get "$page_url" "https://apkcombo.com/" || continue
		page="$html"
		compact_page=$(tr '\n' ' ' <<<"$page")

		dl_url=$(echo "$page" | grep -oP '(?<=a href=")https://download\.apkcombo\.com/[^"]+' | head -1) || true
		[ -z "$dl_url" ] && dl_url=$(echo "$page" | grep -oP '(?<=a href=")/r2[^"]+' | head -1) || true
		[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '"download_url"\s*:\s*"\K[^"]+' | head -1 | sed 's#\\/#/#g') || true
		[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '"url"\s*:\s*"\Khttps://download\.apkcombo\.com/[^"]+' | head -1 | sed 's#\\/#/#g') || true
		[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP 'https://download\.apkcombo\.com/[^"'"'"' <>]+' | head -1 | sed 's#\\/#/#g') || true
		[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '/r2\?u=[^"'"'"' <>]+' | head -1 | sed 's#\\/#/#g') || true

		if [ -n "$dl_url" ]; then
			break
		fi
	done

	if [ -z "$dl_url" ]; then
		for sfx in "${sfxs[@]}"; do
			if [ -n "$version" ]; then
				page_url="${__APKCOMBO_BASE_URL__}/download/phone-${version}-${sfx}"
			else
				page_url="${__APKCOMBO_BASE_URL__}/download/apk"
			fi
			_fs_get "$page_url" "$__APKCOMBO_BASE_URL__" || continue
			page="$html"
			compact_page=$(tr '\n' ' ' <<<"$page")
			dl_url=$(echo "$page" | grep -oP '(?<=a href=")https://download\.apkcombo\.com/[^"]+' | head -1) || true
			[ -z "$dl_url" ] && dl_url=$(echo "$page" | grep -oP '(?<=a href=")/r2[^"]+' | head -1) || true
			[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '"download_url"\s*:\s*"\K[^"]+' | head -1 | sed 's#\\/#/#g') || true
			[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '"url"\s*:\s*"\Khttps://download\.apkcombo\.com/[^"]+' | head -1 | sed 's#\\/#/#g') || true
			[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP 'https://download\.apkcombo\.com/[^"'"'"' <>]+' | head -1 | sed 's#\\/#/#g') || true
			[ -z "$dl_url" ] && dl_url=$(echo "$compact_page" | grep -oP '/r2\?u=[^"'"'"' <>]+' | head -1 | sed 's#\\/#/#g') || true
			if [ -n "$dl_url" ]; then
				break
			fi
		done
	fi

	[ -z "$dl_url" ] && { epr "Could not find APK link on APKCombo"; return 1; }
	[[ "$dl_url" != http* ]] && dl_url="https://apkcombo.com${dl_url}"
	dl_url=$(echo "$dl_url" | sed 's/\\u0026/\&/g; s/&amp;/\&/g')

	if [[ "$dl_url" == https://apkcombo.com/r2\?u=* ]]; then
		final_url=$(python - <<'PYC' "$dl_url"
import sys, urllib.parse
u=sys.argv[1]
q=urllib.parse.urlparse(u).query
raw=urllib.parse.parse_qs(q).get('u',[''])[0]
decoded=urllib.parse.unquote(raw)
parts=urllib.parse.urlsplit(decoded)
query=urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
encoded=urllib.parse.urlunsplit((
    parts.scheme,
    parts.netloc,
    parts.path,
    urllib.parse.urlencode(query, doseq=True, safe='/:_-.'),
    parts.fragment,
))
print(encoded)
PYC
		) || return 1
	else
		checkin=$(req "https://apkcombo.com/checkin" -) || true
		if [ -n "$checkin" ] && [[ "$dl_url" != *fp=* ]]; then
			if [[ "$dl_url" == *\?* ]]; then
				dl_url="${dl_url}&${checkin}"
			else
				dl_url="${dl_url}?${checkin}"
			fi
		fi
		final_url=$(curl -s -o /dev/null -w "%{url_effective}" -L --max-redirs 10 \
			-H "User-Agent: ${user_agent:-Mozilla/5.0}" \
			-H "Referer: $page_url" "$dl_url") || return 1
	fi

	pr "Downloading from APKCombo: $final_url"
	curl -L --fail -s -S --connect-timeout 30 --max-time 300 \
		-H "User-Agent: ${user_agent:-Mozilla/5.0}" \
		-H "Referer: $page_url" "$final_url" -o "$output" || return 1
	if ! unzip -t "$output" >/dev/null 2>&1; then
		epr "Downloaded file from APKCombo is not a valid zip"
		return 1
	fi
	if echo "$final_url$dl_url" | grep -qi 'xapk\|\.apks'; then
		_apkpure_install_xapk "$output" "${output}.extracted" || return 1
		mv "${output}.extracted" "$output"
	fi
}

# -------------------- uptodown --------------------
get_uptodown_resp() {
	__UPTODOWN_RESP__=$(req "${1}/versions" -) || return 1
	__UPTODOWN_RESP_PKG__=$(req "${1}/download" -) || return 1
}
get_uptodown_vers() { $HTMLQ --text ".version" <<<"$__UPTODOWN_RESP__"; }
dl_uptodown() {
	local uptodown_dlurl=$1 version=$2 output=$3 arch=$4 _dpi=$5
	if [ "$arch" = "arm-v7a" ]; then arch="armeabi-v7a"; fi

	local apparch=('arm64-v8a, armeabi-v7a, x86_64' 'arm64-v8a, armeabi-v7a, x86, x86_64' 'arm64-v8a, armeabi-v7a')
	if [ "$arch" != all ]; then
		apparch+=("$arch")
	fi

	local op resp data_code
	data_code=$($HTMLQ "#detail-app-name" --attribute data-code <<<"$__UPTODOWN_RESP__")
	local versionURL=""
	local is_bundle=false
	for i in {1..20}; do
		resp=$(req "${uptodown_dlurl}/apps/${data_code}/versions/${i}" -)
		if ! op=$(jq -e -r ".data | map(select(.version == \"${version}\")) | .[0]" <<<"$resp"); then
			continue
		fi
		if [ "$(jq -e -r ".kindFile" <<<"$op")" = "xapk" ]; then is_bundle=true; fi
		if versionURL=$(jq -e -r '.versionURL' <<<"$op"); then break; else return 1; fi
	done
	if [ -z "$versionURL" ]; then return 1; fi
	versionURL=$(jq -e -r '.url + "/" + .extraURL + "/" + (.versionID | tostring)' <<<"$versionURL")
	resp=$(req "$versionURL" -) || return 1

	local data_version files node_arch="" data_file_id node_class
	data_version=$($HTMLQ '.button.variants' --attribute data-version <<<"$resp") || return 1
	if [ "$data_version" ]; then
		files=$(req "${uptodown_dlurl%/*}/app/${data_code}/version/${data_version}/files" - | jq -e -r .content) || return 1
		for ((n = 1; n < 12; n += 1)); do
			node_class=$($HTMLQ -w -t ".content > :nth-child($n)" --attribute class <<<"$files") || return 1
			if [ "$node_class" != "variant" ]; then
				node_arch=$($HTMLQ -w -t ".content > :nth-child($n)" <<<"$files" | xargs) || return 1
				continue
			fi
			if [ -z "$node_arch" ]; then return 1; fi
			if ! isoneof "$node_arch" "${apparch[@]}"; then continue; fi

			file_type=$($HTMLQ -w -t ".content > :nth-child($n) > .v-file > span" <<<"$files") || return 1
			if [ "$file_type" = "xapk" ]; then is_bundle=true; else is_bundle=false; fi
			data_file_id=$($HTMLQ ".content > :nth-child($n) > .v-report" --attribute data-file-id <<<"$files") || return 1
			resp=$(req "${uptodown_dlurl}/download/${data_file_id}-x" -)
			break
		done
		if [ $n -eq 12 ]; then return 1; fi
	fi
	local data_url
	data_url=$($HTMLQ "#detail-download-button" --attribute data-url <<<"$resp") || return 1
	if [ $is_bundle = true ]; then
		req "https://dw.uptodown.com/dwn/${data_url}" "$output.apkm" || return 1
		merge_splits "${output}.apkm" "${output}"
	else
		req "https://dw.uptodown.com/dwn/${data_url}" "$output"
	fi
}
get_uptodown_pkg_name() { $HTMLQ --text "tr.full:nth-child(1) > td:nth-child(3)" <<<"$__UPTODOWN_RESP_PKG__"; }

# -------------------- archive --------------------
dl_archive() {
	local url=$1 version=$2 output=$3 arch=$4
	local path="" version_f=${version// /}
	while IFS= read -r p; do
		case "$p" in
			*"${version_f#v}-${arch// /}.apk"|*"${version_f#v}-${arch// /}.apkm"|*"${version_f#v}-${arch// /}.xapk"|*"${version_f#v}-${arch// /}.apks"|*"${version_f#v}-all.apk"|*"${version_f#v}-all.apkm"|*"${version_f#v}-all.xapk"|*"${version_f#v}-all.apks")
				path="$p"
				break
				;;
		esac
	done <<<"$__ARCHIVE_RESP__"
	if [ -z "$path" ]; then
		epr "Version ${version} with arch ${arch} not found in archive"
		return 1
	fi
	case "${path##*.}" in
		apk)
			req "${url}/${path}" "$output"
			;;
		apkm|xapk|apks)
			req "${url}/${path}" "${output}.${path##*.}" || return 1
			merge_splits "${output}.${path##*.}" "${output}"
			;;
		*)
			epr "Unsupported archive file type for ${path}"
			return 1
			;;
	esac
}
get_archive_resp() {
	local r
	r=$(req "$1" -)
	if [ -z "$r" ]; then return 1; else __ARCHIVE_RESP__=$(sed -n 's;^<a href="\(.*\)"[^"]*;\1;p' <<<"$r"); fi
	__ARCHIVE_PKG_NAME__=$(awk -F/ '{print $NF}' <<<"$1")
}
get_archive_vers() { sed 's/^[^-]*-//;s/-\(all\|arm64-v8a\|arm-v7a\|x86\|x86_64\)\.\(apk\|apkm\|xapk\|apks\)$//g' <<<"$__ARCHIVE_RESP__"; }
get_archive_pkg_name() { echo "$__ARCHIVE_PKG_NAME__"; }

# -------------------- github --------------------
dl_github() {
    local url=$1 version=$2 output=$3 arch=$4
    local path="" version_f=${version// /}
	local base_url=${__GITHUB_URL__:-$url}
    
    # Matches the exact file selection logic from dl_archive
    while IFS= read -r p; do
        case "$p" in
            *"${version_f#v}-${arch// /}.apk"|*"${version_f#v}-${arch// /}.apkm"|*"${version_f#v}-${arch// /}.xapk"|*"${version_f#v}-${arch// /}.apks"|*"${version_f#v}-all.apk"|*"${version_f#v}-all.apkm"|*"${version_f#v}-all.xapk"|*"${version_f#v}-all.apks")
                path="$p"
                break
                ;;
        esac
    done <<<"$__ARCHIVE_RESP__"
    
    if [ -z "$path" ]; then
        epr "Version ${version} with arch ${arch} not found in github"
        return 1
    fi
    
    local ext="${path##*.}"
    case "$ext" in
        apk)
            req "${base_url}/${path}" "$output"
            ;;
        apkm|xapk|apks)
			local bundle="${output}.${ext}"
			req "${base_url}/${path}" "$bundle" || return 1
			merge_splits "$bundle" "$output"
            ;;
        *)
            epr "Unsupported github file type for ${path}"
            return 1
            ;;
    esac
}

get_github_resp() {
    local repo tag resp
    
    repo=$(cut -d/ -f4-5 <<<"$1")
    tag=${1%/}
    tag=${tag##*/}
    
    resp=$(gh_req "https://api.github.com/repos/${repo}/releases/tags/${tag}" -) || return 1
    
    # Extract only supported file extensions
    __ARCHIVE_RESP__=$(jq -r '.assets[]? | select(.name | test("\\.(apk|apkm|xapk|apks)$")) | .name' <<<"$resp")
    if [ -z "$__ARCHIVE_RESP__" ]; then return 1; fi
    
    # Grab the package name exactly like how get_archive_vers isolates the version
    __ARCHIVE_PKG_NAME__=$(get_github_pkg_name)
    if [ -z "$__ARCHIVE_PKG_NAME__" ]; then return 1; fi
    
    __GITHUB_URL__="https://github.com/${repo}/releases/download/${tag}"
}

# Extracts version matching the archive logic: strips prefix (up to first '-') and suffix (arch/extension)
get_github_vers() {
    sed 's/^[^-]*-//;s/-\(all\|arm64-v8a\|arm-v7a\|x86\|x86_64\)\.\(apk\|apkm\|xapk\|apks\)$//g' <<<"$__ARCHIVE_RESP__"
}

# Extracts package name by stripping everything from the first hyphen '-' onwards
get_github_pkg_name() {
    sed 's/-.*//' <<<"$__ARCHIVE_RESP__" | head -n 1
}

# -------------------- direct --------------------
dl_direct() {
	local url=$1 version=${2// /-} output=$3 arch=$4 _dpi=$5
	case "${url##*.}" in
		apk) req "$url" "${output}" || return 1 ;;
		apkm|xapk|apks)
			local bundle="${output}.${url##*.}"
			req "$url" "$bundle" || return 1
			merge_splits "$bundle" "$output"
			;;
		*) epr "Unsupported direct file type: $url"; return 1 ;;
	esac
}

usage() {
	cat >&2 <<'EOF'
Usage:
  resolve-apk.sh <source> <url> <version> <output.apk> <arch> <dpi>
  resolve-apk.sh latest <source> <url>

Sources:
  direct github archive apkmirror uptodown apkpure apkcombo
EOF
}

latest_version() {
	local source=$1 url=$2
	case "$source" in
		direct)
			basename "$url" | cut -d- -f2 | sed 's/\.\(apk\|xapk\|apks\|apkm\)$//'
			;;
		github)
			command -v jq >/dev/null || { epr "jq is required for github source"; return 1; }
			get_github_resp "$url" || return 1
			get_github_vers | head -n 1
			;;
		archive)
			get_archive_resp "$url" || return 1
			get_archive_vers | sort -Vr | head -n 1
			;;
		apkmirror)
			ensure_htmlq
			get_apkmirror_resp "$url" || return 1
			get_apkmirror_vers | tr ' ' '\n' | head -n 1
			;;
		uptodown)
			command -v jq >/dev/null || { epr "jq is required for uptodown source"; return 1; }
			ensure_htmlq
			get_uptodown_resp "$url" || return 1
			get_uptodown_vers | head -n 1
			;;
		apkpure)
			ensure_htmlq
			get_apkpure_resp "$url" || return 1
			get_apkpure_vers | head -n 1
			;;
		apkcombo)
			get_apkcombo_resp "$url" || return 1
			get_apkcombo_vers | head -n 1
			;;
		*) epr "Unsupported source: $source"; return 2 ;;
	esac
}

main() {
	if [ "${1-}" = latest ]; then
		if [ "$#" -lt 3 ]; then usage; exit 2; fi
		latest_version "$2" "$3"
		return
	fi

	if [ "$#" -lt 6 ]; then usage; exit 2; fi
	local source=$1 url=$2 version=$3 output=$4 arch=$5 dpi=$6
	mkdir -p "$(dirname "$output")"

	case "$source" in
		direct)
			dl_direct "$url" "$version" "$output" "$arch" "$dpi"
			;;
		github)
			command -v jq >/dev/null || { epr "jq is required for github source"; return 1; }
			get_github_resp "$url" || return 1
			dl_github "$url" "$version" "$output" "$arch" "$dpi"
			;;
		archive)
			get_archive_resp "$url" || return 1
			dl_archive "$url" "$version" "$output" "$arch" "$dpi"
			;;
		apkmirror)
			ensure_htmlq
			get_apkmirror_resp "$url" || return 1
			dl_apkmirror "$url" "$version" "$output" "$arch" "$dpi"
			;;
		uptodown)
			command -v jq >/dev/null || { epr "jq is required for uptodown source"; return 1; }
			ensure_htmlq
			get_uptodown_resp "$url" || return 1
			dl_uptodown "$url" "$version" "$output" "$arch" "$dpi"
			;;
		apkpure)
			ensure_htmlq
			get_apkpure_resp "$url" || return 1
			dl_apkpure "$url" "$version" "$output" "$arch" "$dpi"
			;;
		apkcombo)
			get_apkcombo_resp "$url" || return 1
			dl_apkcombo "$url" "$version" "$output" "$arch" "$dpi"
			;;
		*)
			epr "Unsupported source: $source"
			usage
			return 2
			;;
	esac

	test -s "$output"
}

main "$@"
