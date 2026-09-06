import re
import asyncio
import base64
import json
import aiohttp
from typing import List, Dict, Tuple
from urllib.parse import urlparse, parse_qs, unquote

from config import SOURCES

# Регулярки — расширяем ALL чтобы покрыть hy2 и hysteria
VLESS_REGEX = re.compile(r'vless://[^\s\n\r\"\'<>]+', re.IGNORECASE)
VMESS_REGEX = re.compile(r'vmess://[^\s\n\r\"\'<>]+', re.IGNORECASE)
TROJAN_REGEX = re.compile(r'trojan://[^\s\n\r\"\'<>]+', re.IGNORECASE)
SS_REGEX = re.compile(r'ss://[^\s\n\r\"\'<>]+', re.IGNORECASE)
HY2_REGEX = re.compile(r'(hysteria2|hy2)://[^\s\n\r\"\'<>]+', re.IGNORECASE)
TUIC_REGEX = re.compile(r'tuic://[^\s\n\r\"\'<>]+', re.IGNORECASE)

# Все протоколы — vless, vmess, trojan, ss, ssr, hysteria2/hy2, tuic
ALL_REGEX = re.compile(r'(vless|vmess|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s\n\r\"\'<>]+', re.IGNORECASE)

UUID_REGEX = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (VLESS-Parser-Bot/1.0)",
}

BAD_HOSTS = {"127.0.0.1", "0.0.0.0", "localhost", "::1", "255.255.255.255"}

def is_bad_host(host: str) -> bool:
    if not host:
        return True
    h = host.strip().lower()
    if h in BAD_HOSTS:
        return True
    if h == "localhost" or h.endswith(".local"):
        return True
    if h.startswith("10."):
        return True
    if h.startswith("192.168."):
        return True
    if h.startswith("127."):
        return True
    if h.startswith("0."):
        return True
    if h.startswith("172."):
        try:
            second = int(h.split(".")[1])
            if 16 <= second <= 31:
                return True
        except:
            pass
    # also filter hostnames that look like private? Keep domains
    return False

async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    try:
        async with session.get(url, headers=HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                text = await resp.text()
                # Если файл base64 (без ://), пробуем декодировать
                lowered = text.lower()
                has_proto = any(p in lowered for p in ["vless://", "vmess://", "trojan://", "ss://", "hysteria", "hy2://", "tuic://"])
                if not has_proto and len(text.strip()) > 50:
                    stripped = text.strip().replace("\n","").replace("\r","").replace(" ","")
                    # base64-like?
                    if re.match(r'^[A-Za-z0-9+/=\n\r]+$', stripped) and len(stripped) % 4 <= 2:
                        try:
                            decoded = base64.b64decode(stripped + "==" ).decode('utf-8', errors='ignore')
                            if "://" in decoded:
                                return decoded
                        except Exception:
                            pass
                        # try line-by-line base64 (some lists encode each line)
                        try:
                            lines = text.strip().splitlines()
                            decoded_lines = []
                            any_decoded=False
                            for ln in lines:
                                ln=ln.strip()
                                if not ln or ln.startswith("#"):
                                    continue
                                try:
                                    d=base64.b64decode(ln + "==").decode('utf-8', errors='ignore')
                                    if "://" in d:
                                        decoded_lines.append(d)
                                        any_decoded=True
                                    else:
                                        decoded_lines.append(ln)
                                except:
                                    decoded_lines.append(ln)
                            if any_decoded:
                                return "\n".join(decoded_lines)
                        except:
                            pass
                return text
            else:
                print(f"[fetch] {url} -> {resp.status}")
                return ""
    except Exception as e:
        print(f"[fetch error] {url}: {e}")
        return ""

def extract_configs(text: str, proto_filter: str = "all") -> List[str]:
    """Извлекает конфиги нужного протокола. Если proto_filter == 'all' — все."""
    if not text:
        return []
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    clean_text = "\n".join(lines)
    
    if proto_filter == "vless":
        found = VLESS_REGEX.findall(clean_text)
    elif proto_filter == "all":
        found = [m.group(0) for m in ALL_REGEX.finditer(clean_text)]
        # Also try to catch ss:// that may be base64-encoded in one line without proper prefix handled above
        # If still empty and text looks like base64 blob, already decoded in fetch_text
    else:
        found = VLESS_REGEX.findall(clean_text)
    
    seen = set()
    uniq = []
    for f in found:
        f = f.strip()
        f = f.rstrip('.,;\'")')
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq

def is_valid_vless(link: str) -> Tuple[bool, str]:
    try:
        if not link.lower().startswith("vless://"):
            return False, "не vless://"
        without_scheme = link[8:]
        if "#" in without_scheme:
            without_scheme, _ = without_scheme.split("#", 1)
        if "?" in without_scheme:
            host_part, query = without_scheme.split("?", 1)
            params = parse_qs(query)
        else:
            host_part = without_scheme
            params = {}
        if "@" not in host_part:
            return False, "нет @ (uuid@host)"
        uuid_part, hostport = host_part.rsplit("@", 1)
        uuid_part = unquote(uuid_part)
        if not UUID_REGEX.match(uuid_part):
            return False, f"невалидный UUID: {uuid_part[:12]}"
        if ":" not in hostport:
            return False, "нет порта host:port"
        host, port_str = hostport.rsplit(":", 1)
        host = host.strip("[]")
        if is_bad_host(host):
            return False, f"bad host {host}"
        try:
            port = int(port_str)
            if not (1 <= port <= 65535):
                return False, f"порт вне диапазона: {port}"
        except:
            return False, f"невалидный порт: {port_str}"
        # reality params not strictly required but check sni if security=reality
        return True, "ok"
    except Exception as e:
        return False, str(e)

def is_valid_trojan(link: str) -> Tuple[bool, str]:
    try:
        if not link.lower().startswith("trojan://"):
            return False, "не trojan"
        without = link[9:] if link.lower().startswith("trojan://") else link.split("://",1)[1]
        if "#" in without:
            without = without.split("#",1)[0]
        if "?" in without:
            without = without.split("?",1)[0]
        if "@" not in without:
            return False, "нет @"
        pwd, hostport = without.rsplit("@",1)
        if not pwd:
            return False, "пустой пароль"
        if ":" not in hostport:
            return False, "нет порта"
        host, port_str = hostport.rsplit(":",1)
        host=host.strip("[]")
        if is_bad_host(host):
            return False, f"bad host {host}"
        try:
            port=int(port_str)
            if not 1 <= port <= 65535:
                return False, "bad port"
        except:
            return False, "bad port"
        if len(link) < 20:
            return False, "too short"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def is_valid_ss(link: str) -> Tuple[bool, str]:
    try:
        l=link.strip()
        if not l.lower().startswith("ss://"):
            return False, "не ss"
        payload = l[5:]
        # strip fragment and query
        if "#" in payload:
            payload = payload.split("#",1)[0]
        # ss format complexities:
        # 1) ss://method:password@host:port
        # 2) ss://BASE64 (which decodes to method:password@host:port)
        # 3) ss://BASE64@host:port  (not common)
        # Detect if "@" in payload and ":" before @ -> likely plaintext method:password
        if "@" in payload:
            # check if before @ contains ":" -> method:password
            before_at = payload.rsplit("@",1)[0]
            hostport = payload.rsplit("@",1)[1]
            # hostport should be host:port (maybe with ?plugin)
            if "?" in hostport:
                hostport = hostport.split("?",1)[0]
            if ":" not in hostport:
                return False, "нет порта в ss"
            host, port_str = hostport.rsplit(":",1)
            host=host.strip("[]")
            if is_bad_host(host):
                return False, f"bad host {host}"
            try:
                port=int(port_str)
                if not 1 <= port <= 65535:
                    return False, "bad port"
            except:
                return False, "bad port"
            # method check: before_at should decode or contain ":"
            if ":" not in before_at:
                # maybe base64 encoded method:password
                try:
                    decoded = base64.b64decode(before_at + "==").decode('utf-8', errors='ignore')
                    if ":" not in decoded:
                        return False, "bad method"
                except:
                    return False, "bad method b64"
            return True, "ok"
        else:
            # no @, likely base64 encoded whole method:password@host:port
            # need to decode
            b64part = payload.split("?",1)[0].strip()
            # remove possible trailing =
            try:
                decoded = base64.b64decode(b64part + "==").decode('utf-8', errors='ignore')
            except:
                return False, "bad b64 ss"
            if "@" not in decoded or ":" not in decoded:
                return False, "bad decoded ss"
            # decoded should be method:password@host:port
            try:
                hostport = decoded.rsplit("@",1)[1]
                if ":" not in hostport:
                    return False, "no port in decoded"
                host, port_str = hostport.rsplit(":",1)
                host=host.strip()
                if is_bad_host(host):
                    return False, f"bad host {host}"
                port=int(port_str.split("?")[0].split("/")[0])
                if not 1 <= port <= 65535:
                    return False, "bad port"
            except Exception as e:
                return False, f"decoded host fail {e}"
            return True, "ok"
    except Exception as e:
        return False, str(e)

def is_valid_vmess(link: str) -> Tuple[bool, str]:
    try:
        if not link.lower().startswith("vmess://"):
            return False, "не vmess"
        b64part = link[8:].split("#",1)[0].split("?",1)[0].strip()
        if not b64part:
            return False, "пустой vmess"
        # vmess payload is base64 json
        # add padding
        pad = len(b64part) % 4
        if pad:
            b64part += "=" * (4-pad)
        try:
            decoded = base64.b64decode(b64part).decode('utf-8', errors='ignore')
        except:
            # try urlsafe
            try:
                decoded = base64.urlsafe_b64decode(b64part).decode('utf-8', errors='ignore')
            except Exception as e:
                return False, f"bad b64 vmess {e}"
        # decoded should be json
        if not decoded.strip().startswith("{"):
            return False, "not json"
        try:
            obj = json.loads(decoded)
        except:
            return False, "bad json"
        # required fields
        host = obj.get("add") or obj.get("host") or obj.get("address")
        port = obj.get("port")
        uid = obj.get("id")
        if not host or is_bad_host(str(host).strip()):
            return False, "bad add"
        try:
            p = int(str(port))
            if not 1 <= p <= 65535:
                return False, "bad port"
        except:
            return False, "bad port vmess"
        if uid and not UUID_REGEX.match(str(uid).strip()):
            # some vmess use not uuid but still allow? strict: fail
            # but allow non-uuid for compatibility
            pass
        # check net type
        return True, "ok"
    except Exception as e:
        return False, str(e)

def is_valid_hysteria2(link: str) -> Tuple[bool, str]:
    try:
        low = link.lower()
        if not (low.startswith("hysteria2://") or low.startswith("hy2://")):
            return False, "не hy2"
        # strip scheme
        without = link.split("://",1)[1]
        if "#" in without:
            without = without.split("#",1)[0]
        if "?" in without:
            without = without.split("?",1)[0]
        # format: password@host:port  or host:port?password
        # hy2 typical: hy2://password@host:port/?...
        if "@" in without:
            pwd, hostport = without.rsplit("@",1)
            if not pwd:
                return False, "no pwd"
            if ":" not in hostport:
                return False, "no port"
            host, port_str = hostport.rsplit(":",1)
        else:
            # maybe without password, just host:port
            if ":" not in without:
                return False, "no port hy2"
            host, port_str = without.rsplit(":",1)
        host=host.strip("[]")
        if is_bad_host(host):
            return False, f"bad host {host}"
        try:
            port=int(port_str)
            if not 1 <= port <= 65535:
                return False, "bad port"
        except:
            return False, "bad port hy2"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def is_valid_tuic(link: str) -> Tuple[bool, str]:
    try:
        if not link.lower().startswith("tuic://"):
            return False, "не tuic"
        without = link.split("://",1)[1]
        if "#" in without:
            without = without.split("#",1)[0]
        if "?" in without:
            without = without.split("?",1)[0]
        if "@" not in without:
            return False, "нет @ tuic"
        userpass, hostport = without.rsplit("@",1)
        if ":" not in hostport:
            return False, "нет порта tuic"
        host, port_str = hostport.rsplit(":",1)
        host=host.strip("[]")
        if is_bad_host(host):
            return False, f"bad host {host}"
        try:
            port=int(port_str)
            if not 1 <= port <= 65535:
                return False, "bad port"
        except:
            return False, "bad port tuic"
        return True, "ok"
    except Exception as e:
        return False, str(e)

def extract_host_port(link: str):
    """Пытается извлечь host,port для любого протокола. Возвращает (host,port) или None."""
    try:
        low = link.lower()
        if low.startswith("vless://"):
            tmp = link[8:].split("#",1)[0].split("?",1)[0]
            if "@" not in tmp:
                return None
            hostport = tmp.rsplit("@",1)[1]
            if ":" not in hostport:
                return None
            host, port_str = hostport.rsplit(":",1)
            return host.strip("[]"), int(port_str)
        if low.startswith("trojan://"):
            tmp = link.split("://",1)[1].split("#",1)[0].split("?",1)[0]
            if "@" not in tmp:
                return None
            hostport = tmp.rsplit("@",1)[1]
            if ":" not in hostport:
                return None
            host, port_str = hostport.rsplit(":",1)
            return host.strip("[]"), int(port_str)
        if low.startswith("ss://"):
            # use ss validator logic to decode
            l=link.strip()
            payload = l[5:].split("#",1)[0]
            if "@" in payload:
                hostport = payload.rsplit("@",1)[1].split("?",1)[0]
                if ":" not in hostport:
                    return None
                host, port_str = hostport.rsplit(":",1)
                return host.strip("[]"), int(port_str)
            else:
                b64part = payload.split("?",1)[0]
                try:
                    decoded = base64.b64decode(b64part + "==").decode('utf-8', errors='ignore')
                    if "@" in decoded:
                        hostport = decoded.rsplit("@",1)[1].split("?",1)[0]
                        if ":" not in hostport:
                            return None
                        host, port_str = hostport.rsplit(":",1)
                        return host.strip("[]"), int(port_str)
                except:
                    pass
                return None
        if low.startswith("vmess://"):
            b64part = link[8:].split("#",1)[0].strip()
            pad = len(b64part) % 4
            if pad:
                b64part += "="*(4-pad)
            try:
                decoded = base64.b64decode(b64part).decode('utf-8', errors='ignore')
                obj=json.loads(decoded)
                host=obj.get("add")
                port=obj.get("port")
                if host and port:
                    return str(host).strip(), int(str(port).strip())
            except:
                pass
            return None
        if low.startswith("hysteria2://") or low.startswith("hy2://"):
            tmp = link.split("://",1)[1].split("#",1)[0].split("?",1)[0]
            if "@" in tmp:
                hostport = tmp.rsplit("@",1)[1]
            else:
                hostport = tmp
            if ":" not in hostport:
                return None
            host, port_str = hostport.rsplit(":",1)
            return host.strip("[]"), int(port_str)
        if low.startswith("tuic://"):
            tmp = link.split("://",1)[1].split("#",1)[0].split("?",1)[0]
            if "@" not in tmp:
                return None
            hostport = tmp.rsplit("@",1)[1]
            if ":" not in hostport:
                return None
            host, port_str = hostport.rsplit(":",1)
            return host.strip("[]"), int(port_str)
        # generic fallback: try to find @host:port pattern
        if "@" in link and ":" in link:
            # find last @ ... :port
            m = re.search(r'@([^:@\s/?#]+):(\d+)', link)
            if m:
                return m.group(1), int(m.group(2))
        return None
    except:
        return None

async def check_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except:
            pass
        return True
    except:
        return False

def is_valid_any(link: str) -> Tuple[bool, str]:
    # Теперь только VLESS (по ТЗ)
    low = link.lower().strip()
    if low.startswith("vless://"):
        return is_valid_vless(link)
    # Все остальные протоколы вырезаны — считаем невалидными
    return False, "only vless allowed"

async def validate_configs(links: List[str], mode: str = "syntax", allow_generic: bool = False) -> List[str]:
    """Фильтрует невалидные конфиги. mode: none | syntax | tcp"""
    if mode == "none":
        return links
    # First syntax pass
    valid = []
    for link in links:
        ok, reason = is_valid_any(link)
        if not ok:
            continue
        # additional generic bad-host filter (private etc) already inside validators
        valid.append(link)
    if mode == "tcp":
        # TCP check with concurrency limit
        sem = asyncio.Semaphore(80)
        results = []
        async def check_one(l):
            async with sem:
                hp = extract_host_port(l)
                if not hp:
                    return None  # keep? For vmess etc host extraction may fail — keep by syntax only
                host, port = hp
                # skip obviously not checkable? e.g., host is domain -> try tcp
                ok = await check_tcp(host, port, timeout=2.5)
                return l if ok else None
        tasks = [check_one(l) for l in valid]
        checked = await asyncio.gather(*tasks)
        # keep those where check passed or where host not extractable (None)
        # For None (host not extracted) we keep original?
        # Our check_one returns None for fail, or l for success, or None for no host -> ambiguous. Rework: return marker
        # Simpler: above returns l if ok else None, but for no-host case we returned None earlier meaning we filter out those without host?
        # For vmess without host we already filtered syntax, but host extraction should succeed.
        # Let's instead treat no-host as keep.
        # We need to distinguish.
        # Redo logic: if hp is None -> keep (return l)
        # So adjust: check_one should return l if hp None
        # We'll recompute correctly below if needed
        # For now, handle: keep only where checked is not None
        # But that would drop vmess where hp extraction failed even though syntax passed — not ideal.
        # Implement fix: redo tcp filtering with explicit host check
        filtered = []
        for orig, res in zip(valid, checked):
            if res is not None:
                filtered.append(res)
            else:
                # check if host was None originally — then keep orig
                hp = extract_host_port(orig)
                if hp is None:
                    filtered.append(orig)
                # else it failed tcp -> drop
        valid = filtered
    return valid

async def fetch_category(session: aiohttp.ClientSession, category_key: str, mode: str = "syntax") -> Dict:
    cfg = SOURCES.get(category_key)
    if not cfg:
        return {"key": category_key, "name": category_key, "configs": [], "raw_text": "", "error": "unknown category"}
    
    # Теперь только VLESS (по ТЗ вырезать все остальные протоколы)
    proto = "vless"
    
    all_configs = []
    raw_parts = []
    errors = []
    
    for url in cfg["urls"]:
        text = await fetch_text(session, url)
        if not text:
            errors.append(f"{url} — пусто/ошибка")
            continue
        raw_parts.append(f"# Source: {url}\n{text.strip()}\n")
        configs = extract_configs(text, proto_filter=proto)
        all_configs.extend(configs)
    
    # Дедуп до валидации (по строке)
    seen = set()
    uniq = []
    for c in all_configs:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    
    raw_total = len(uniq)
    # Валидация — строгая по всем протоколам
    filtered = await validate_configs(uniq, mode=mode, allow_generic=True)
    
    deduped = filtered
    
    removed = raw_total - len(deduped)
    if removed>0:
        print(f"[{category_key}] очистка: {raw_total} -> {len(deduped)} (удалено {removed}: невалидные/приватные)")
    
    return {
        "key": category_key,
        "name": cfg["name"],
        "description": cfg["description"],
        "configs": deduped,
        "raw_total": raw_total,
        "filtered_total": len(deduped),
        "removed": removed,
        "errors": errors,
        "urls": cfg["urls"],
        "raw_text": "\n".join(raw_parts)[:200000],
    }

async def fetch_all(mode: str = "syntax", categories: List[str] = None) -> Dict[str, Dict]:
    if categories is None:
        categories = list(SOURCES.keys())
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_category(session, k, mode=mode) for k in categories]
        results = await asyncio.gather(*tasks)
    return {r["key"]: r for r in results}

def make_subscription_content(configs: List[str], header: str = "") -> str:
    lines = []
    if header:
        lines.append(header)
    lines.extend(configs)
    return "\n".join(lines)

def make_base64_subscription(configs: List[str], header: str = "") -> str:
    content = make_subscription_content(configs, header)
    b64 = base64.b64encode(content.encode('utf-8')).decode('utf-8')
    return b64

def parse_vless_info(link: str) -> Dict:
    try:
        remark = ""
        if "#" in link:
            remark = unquote(link.split("#", 1)[1])
        tmp = link[8:].split("#", 1)[0].split("?", 1)[0]
        uuid_part, hostport = tmp.rsplit("@", 1)
        host, port = hostport.rsplit(":", 1)
        params = {}
        if "?" in link:
            q = link.split("?", 1)[1].split("#", 1)[0]
            params = parse_qs(q)
        def get(k, d=""):
            return params.get(k, [d])[0]
        return {
            "remark": remark or f"{host}:{port}",
            "host": host,
            "port": port,
            "uuid": uuid_part,
            "sni": get("sni", get("serverName", "")),
            "type": get("type", ""),
            "security": get("security", ""),
            "fp": get("fp", ""),
        }
    except Exception as e:
        return {"remark": link[:40], "host": "?", "port": "?", "error": str(e)}
