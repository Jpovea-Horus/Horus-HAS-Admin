"""Tarjetas Lovelace sin HACS: JS en /config/www/<slug>/ + recurso en .storage/lovelace_resources."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from exceptions import SSHCommandError, ValidationError
from models import LovelaceCardStatus

if TYPE_CHECKING:
    from ha_config_manager import HaConfigManager
    from ssh_client import SSHClient


@dataclass(frozen=True)
class CardSpec:
    name: str
    slug: str
    js_name: str
    repo: str

    @property
    def release_url(self) -> str:
        return f"https://github.com/{self.repo}/releases/latest/download/{self.js_name}"

    @property
    def api_url(self) -> str:
        return f"https://api.github.com/repos/{self.repo}/releases/latest"

    @property
    def base_url(self) -> str:
        return f"/local/{self.slug}/{self.js_name}"


CAMERA_GALLERY = CardSpec(
    name="Camera Gallery Card",
    slug="camera-gallery-card",
    js_name="camera-gallery-card.js",
    repo="TheScubaDiver/camera-gallery-card",
)

_STATUS_SCRIPT = r"""
import json,os,re
cfg,slug,js,base=@ARGS@
res_path=os.path.join(cfg,'.storage','lovelace_resources')
out={'js_exists':os.path.isfile(os.path.join(cfg,'www',slug,js)),
     'hacs_copy':os.path.isdir(os.path.join(cfg,'www','community',slug)),
     'storage_dir':os.path.isdir(os.path.join(cfg,'.storage')),
     'resource_url':'','other_urls':[],'yaml_mode':False,'error':''}
try:
    items=json.load(open(res_path)).get('data',{}).get('items',[]) if os.path.isfile(res_path) else []
except Exception as e:
    items=[]; out['error']='lovelace_resources ilegible: %s'%e
for it in items:
    url=it.get('url','')
    if url.split('?')[0]==base: out['resource_url']=url
    elif slug in url: out['other_urls'].append(url)
try:
    y=open(os.path.join(cfg,'configuration.yaml')).read()
    m=re.search(r'^lovelace:.*$((?:\n(?:[ \t].*)?)*)',y,re.M)
    out['yaml_mode']=bool(m and re.search(r'^[ \t]+(?:resource_)?mode:\s*yaml\b',m.group(1),re.M))
except Exception:
    pass
print(json.dumps(out))
"""

_REGISTER_SCRIPT = """
import json,os,shutil,uuid
p,url,base,remove=@ARGS@
if os.path.isfile(p):
    shutil.copy2(p,p+'.bak.horus')
    d=json.load(open(p))
else:
    d={'version':1,'minor_version':1,'key':'lovelace_resources','data':{'items':[]}}
data=d.setdefault('data',{})
items=[i for i in data.get('items',[]) if i.get('url','').split('?')[0]!=base]
if not remove:
    items.append({'id':uuid.uuid4().hex,'url':url,'type':'module'})
data['items']=items
tmp=p+'.horus_tmp'
with open(tmp,'w') as fh:
    json.dump(d,fh,indent=2)
os.replace(tmp,p)
print('OK')
"""

_DOWNLOAD_SCRIPT = """
import json,os,sys,tempfile,urllib.request
api,dl=@ARGS@
hdr={'User-Agent':'Horus-HAS-Admin'}
tag=''
try:
    tag=json.load(urllib.request.urlopen(urllib.request.Request(api,headers=hdr),timeout=30)).get('tag_name','')
except Exception:
    pass
data=urllib.request.urlopen(urllib.request.Request(dl,headers=hdr),timeout=120).read()
if len(data)<1024:
    sys.exit('JS descargado vacío o inválido')
fd,path=tempfile.mkstemp(prefix='horus_card_',suffix='.js')
os.write(fd,data)
os.close(fd)
print(tag or '-')
print(path)
"""


def _py(script: str, *args: object) -> str:
    code = script.replace("@ARGS@", repr(args))
    return f"python3 -c {shlex.quote(code)}"


class LovelaceCardManager:
    """Instala una tarjeta Lovelace (CardSpec) descargando de GitHub o subiendo el JS local."""

    def __init__(self, ssh: SSHClient, ha_config: HaConfigManager, spec: CardSpec):
        self.ssh = ssh
        self.ha_config = ha_config
        self.spec = spec

    def _paths(self) -> tuple[str, str, str, str]:
        cfg = self.ha_config._detect_config_dir()
        card_dir = f"{cfg}/www/{self.spec.slug}"
        return (
            cfg,
            card_dir,
            f"{card_dir}/{self.spec.js_name}",
            f"{cfg}/.storage/lovelace_resources",
        )

    def get_status(self) -> LovelaceCardStatus:
        cfg, _, js_path, res_path = self._paths()
        status = LovelaceCardStatus(
            name=self.spec.name,
            repo=self.spec.repo,
            config_dir=cfg,
            js_path=js_path,
            resources_path=res_path,
        )
        res = self.ssh.run(
            _py(_STATUS_SCRIPT, cfg, self.spec.slug, self.spec.js_name, self.spec.base_url),
            use_sudo=True,
        )
        try:
            data = json.loads(res.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            status.error = (res.stderr or res.stdout or "Sin respuesta del controlador").strip()[-300:]
            return status
        status.js_exists = bool(data.get("js_exists"))
        status.hacs_copy = bool(data.get("hacs_copy")) or bool(data.get("other_urls"))
        status.storage_dir_exists = bool(data.get("storage_dir"))
        status.resource_url = data.get("resource_url", "")
        status.resource_registered = bool(status.resource_url)
        status.yaml_resource_mode = bool(data.get("yaml_mode"))
        status.error = data.get("error", "")
        if "?v=" in status.resource_url:
            status.version = status.resource_url.split("?v=", 1)[1]
        return status

    def install_from_github(self, restart: bool = True) -> str:
        res = self.ssh.run(
            _py(_DOWNLOAD_SCRIPT, self.spec.api_url, self.spec.release_url), timeout=180
        )
        lines = res.stdout.strip().splitlines()
        if not res.ok or len(lines) < 2 or not lines[-1].startswith("/tmp/horus_card_"):
            detail = (res.stderr or res.stdout or "").strip()[-400:]
            raise SSHCommandError(
                f"El controlador no pudo descargar {self.spec.name} desde GitHub "
                f"(¿sin salida a internet/DNS?). Use la subida manual. {detail}",
                exit_code=res.exit_code,
                stderr=res.stderr,
            )
        tag = lines[-2].strip()
        version = tag.lstrip("vV") if tag != "-" else ""
        return self._deploy(lines[-1].strip(), version, restart)

    def install_from_local(self, local_path: str, restart: bool = True) -> str:
        local = resolve_card_js(local_path, self.spec.js_name)
        remote_tmp = f"/tmp/horus_card_{self.spec.js_name}"
        sftp = self.ssh.open_sftp()
        try:
            sftp.put(str(local), remote_tmp)
        except Exception as exc:
            raise SSHCommandError(f"No se pudo subir {self.spec.js_name}: {exc}") from exc
        finally:
            sftp.close()
        return self._deploy(remote_tmp, "", restart)

    def remove(self, restart: bool = True) -> str:
        status = self.get_status()
        if not status.js_exists and not status.resource_registered:
            return f"No había {self.spec.name} instalada."
        _, card_dir, _, res_path = self._paths()
        msgs: list[str] = []
        if status.js_exists:
            self.ssh.run(f"rm -rf {shlex.quote(card_dir)}", use_sudo=True)
            msgs.append(f"Eliminado {card_dir}.")
        if status.resource_registered:
            self._write_resource(res_path, "", remove=True)
            msgs.append("Recurso quitado de lovelace_resources.")
        if restart:
            msgs.append(self.ha_config.restart_ha())
        return " ".join(msgs)

    def _deploy(self, remote_tmp: str, version: str, restart: bool) -> str:
        cfg, card_dir, js_path, res_path = self._paths()
        status = self.get_status()
        if not status.storage_dir_exists:
            self.ssh.run(f"rm -f {shlex.quote(remote_tmp)}", use_sudo=True)
            raise ValidationError(
                f"No existe {cfg}/.storage: arranque Home Assistant al menos una vez."
            )

        ref = shlex.quote(f"{cfg}/configuration.yaml")
        q_dir, q_js, q_tmp = shlex.quote(card_dir), shlex.quote(js_path), shlex.quote(remote_tmp)
        place_cmd = (
            f"mkdir -p {q_dir} && cp {q_tmp} {q_js} && "
            f"chown --reference={ref} {q_dir} {q_js} && chmod 644 {q_js}"
        )
        place = self.ssh.run(f"sh -c {shlex.quote(place_cmd)}", use_sudo=True)
        if not version:
            digest = self.ssh.run(f"md5sum {shlex.quote(remote_tmp)}", use_sudo=True)
            version = digest.stdout.split()[0][:8] if digest.ok and digest.stdout else "1"
        self.ssh.run(f"rm -f {shlex.quote(remote_tmp)}", use_sudo=True)
        if not place.ok:
            raise SSHCommandError(
                f"No se pudo instalar {js_path}: {place.stderr or place.stdout}"
            )

        url = f"{self.spec.base_url}?v={version}"
        self._write_resource(res_path, url)
        msgs = [f"{self.spec.name} ({version}) → {js_path}.", f"Recurso registrado: {url}."]
        if status.hacs_copy:
            msgs.append(
                "AVISO: también existe una copia instalada por HACS; elimínela en HACS "
                "para evitar 'custom element already defined'."
            )
        if restart:
            msgs.append(self.ha_config.restart_ha())
        return " ".join(msgs)

    def _write_resource(self, res_path: str, url: str, remove: bool = False) -> None:
        res = self.ssh.run(
            _py(_REGISTER_SCRIPT, res_path, url, self.spec.base_url, remove),
            use_sudo=True,
        )
        if not res.ok or "OK" not in res.stdout:
            raise SSHCommandError(
                f"No se pudo actualizar {res_path}: {(res.stderr or res.stdout).strip()[-300:]}"
            )


def resolve_card_js(local_path: str, js_name: str) -> Path:
    """Acepta el .js o la carpeta que lo contiene."""
    local = Path(local_path).expanduser()
    if local.is_dir():
        candidate = local / js_name
        if candidate.is_file():
            return candidate
        raise ValidationError(f"No existe {js_name} en la carpeta: {local}")
    if local.is_file() and local.suffix.lower() == ".js":
        return local
    raise ValidationError(f"No existe el JS local: {local}")
