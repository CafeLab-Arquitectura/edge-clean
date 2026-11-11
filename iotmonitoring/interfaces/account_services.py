import os
from datetime import timezone

from flask import Blueprint, Response, jsonify, request

from iam.application.services import IamApplicationService
from iotmonitoring.infrastructure.backend_client import (
    BackendAuthError,
    BackendClient,
    BackendError,
    BackendUnavailableError,
)
from iotmonitoring.infrastructure.repositories import (
    ActuatorEventRepository,
    BackendAccountRepository,
    SensorReadingRepository,
    StorageThresholdsRepository,
)
from shared.infrastructure.config import BackendConfig
from shared.infrastructure.sync_worker import worker as sync_worker

onboarding_api = Blueprint("onboarding_api", __name__)

iam_service = IamApplicationService()
reading_repository = SensorReadingRepository()

DEFAULT_BACKEND_URL = os.environ.get(
    "BACKEND_BASE_URL", "https://includes-glasses-instruments-antonio.trycloudflare.com"
)


def _normalize_lot_id(value) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or normalized.lower() in {"null", "none"}:
        return None
    return normalized


def _read_backend_lot_id(lot: dict) -> str | None:
    return _normalize_lot_id(
        lot.get("id") or lot.get("coffeeLotId") or lot.get("coffee_lot_id")
    )


def _format_dt(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _device_view(device) -> dict:
    latest = reading_repository.find_latest_by_device_id(device.device_id)
    normalized_lot_id = _normalize_lot_id(device.lot_id)
    return {
        "deviceId": device.device_id,
        "lotId": normalized_lot_id,
        "assigned": normalized_lot_id is not None,
        "readingCount": reading_repository.count_by_device(device.device_id),
        "lastSeenAt": _format_dt(latest.recorded_at) if latest else None,
    }


@onboarding_api.route("/api/v1/edge/account", methods=["GET"])
def get_account():
    account = BackendAccountRepository.get()
    if account is None:
        return jsonify({"configured": False}), 200
    return jsonify({
        "configured": True,
        "email": account.email,
        "backendUrl": account.base_url,
    }), 200


@onboarding_api.route("/api/v1/edge/account", methods=["POST"])
def set_account():
    data = request.get_json(silent=True) or {}
    email = data.get("email")
    password = data.get("password")
    backend_url = (
        data.get("backendUrl")
        or data.get("backend_url")
        or os.environ.get("BACKEND_BASE_URL", DEFAULT_BACKEND_URL)
    )

    if not email or not password:
        return jsonify({"error": "email y password son requeridos"}), 400

    client = BackendClient(BackendConfig(base_url=backend_url, service_email=email, service_password=password))
    try:
        client.sign_in()
    except BackendAuthError:
        return jsonify({"error": "Credenciales invalidas"}), 401
    except BackendUnavailableError as error:
        return jsonify({"error": f"No se pudo contactar el backend: {error}"}), 502

    BackendAccountRepository.save(backend_url, email, password)
    sync_worker.start()

    return jsonify({"configured": True, "email": email, "backendUrl": backend_url}), 200


@onboarding_api.route("/api/v1/edge/devices", methods=["GET"])
def list_devices():
    devices = [_device_view(d) for d in iam_service.get_all_devices()]
    devices.sort(key=lambda d: (d["assigned"], d["deviceId"]))
    return jsonify({"devices": devices}), 200


@onboarding_api.route("/api/v1/edge/lots", methods=["GET"])
def list_lots():
    if BackendAccountRepository.get() is None:
        return jsonify({"error": "El edge no esta vinculado a una cuenta"}), 409
    try:
        lots = BackendClient().get_coffee_lots()
    except BackendError as error:
        return jsonify({"error": f"No se pudieron obtener los lotes: {error}"}), 502

    simplified = [
        {
            "id": _read_backend_lot_id(lot),
            "lotName": lot.get("lotName") or lot.get("lot_name"),
            "coffeeType": lot.get("coffeeType") or lot.get("coffee_type"),
            "status": lot.get("status"),
        }
        for lot in lots
    ]
    return jsonify({"lots": simplified}), 200


@onboarding_api.route("/api/v1/edge/devices/<device_id>/assign", methods=["POST"])
def assign_lot(device_id):
    data = request.get_json(silent=True) or {}
    lot_id = _normalize_lot_id(data.get("lotId") or data.get("lot_id"))
    if lot_id is None:
        return jsonify({"error": "lotId es requerido"}), 400

    try:
        device = iam_service.assign_lot(device_id, str(lot_id))
    except ValueError:
        return jsonify({"error": "Dispositivo no encontrado"}), 404

    sync_worker.notify()
    return jsonify(_device_view(device)), 200


@onboarding_api.route("/api/v1/edge/devices/reset", methods=["POST"])
def reset_devices():
    deleted = iam_service.reset_devices()
    SensorReadingRepository.delete_all()
    ActuatorEventRepository.delete_all()
    StorageThresholdsRepository.delete_all()
    return jsonify({"devicesDeleted": deleted}), 200


ONBOARDING_HTML = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CafeLab Edge - Configuracion</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 44em; margin: 2em auto; padding: 0 1em; }
    h2 { margin-top: 2em; border-top: 1px solid #ddd; padding-top: 1em; }
    label { display: block; margin: .8em 0 .2em; font-weight: 600; }
    input, select { width: 100%; padding: .5em; box-sizing: border-box; }
    button { margin-top: .8em; padding: .6em 1em; cursor: pointer; }
    .full { width: 100%; }
    .msg { margin-top: 1em; }
    .ok { color: #137333; }
    .err { color: #b00020; }
    table { width: 100%; border-collapse: collapse; margin-top: 1em; }
    th, td { text-align: left; padding: .4em .3em; border-bottom: 1px solid #eee; font-size: .9em; vertical-align: middle; }
    .pill { font-size: .75em; padding: .1em .5em; border-radius: 1em; }
    .pill.pend { background: #fde7c9; color: #8a4b00; }
    .pill.ok { background: #d6f0dd; color: #137333; }
    .row-actions { display: flex; gap: .4em; align-items: center; }
    .danger { color: #b00020; border-color: #b00020; }
    .muted { color: #777; font-size: .85em; }
    .help { margin: .4em 0 1em; color: #555; font-size: .9em; }
    .device-note { display: block; color: #666; font-size: .8em; margin-top: .25em; }
  </style>
</head>
<body>
  <h1>Configuración de Tracksilo</h1>

  <h2>1. Vincular a tu cuenta</h2>
  <p id="acct" class="msg muted">Verificando cuenta...</p>
  <form id="f">
    <label>Email</label>
    <input id="email" type="email" required>
    <label>Contrasena</label>
    <input id="password" type="password" required>
    <label>URL del backend</label>
    <input id="backendUrl" type="url" placeholder="https://includes-glasses-instruments-antonio.trycloudflare.com">
    <button type="submit" class="full">Vincular</button>
  </form>
  <p id="msg" class="msg"></p>

  <h2>2. Dispositivos detectados</h2>
  <p class="muted">Los IoT que se anuncian en la red local aparecen aqui. Asignale a cada uno un lote para que sus lecturas se sincronicen.</p>
  <p class="help">Estado `pendiente`: el dispositivo ya llego al edge, pero todavia no tiene un lote asignado. Estado `asignado`: ya tiene lote y puede sincronizar con el backend.</p>
  <button id="refresh" type="button">Actualizar lista</button>
  <table id="devices">
    <thead><tr><th>Dispositivo</th><th>Visto</th><th>Lecturas locales</th><th>Lote</th></tr></thead>
    <tbody></tbody>
  </table>
  <p id="dmsg" class="msg"></p>
  <p class="help">`Actualizar lista` vuelve a consultar dispositivos y lotes del backend. `Borrar dispositivos locales y lecturas` limpia del edge los dispositivos anunciados, sus lecturas y sus umbrales locales, pero mantiene la cuenta vinculada.</p>
  <button id="reset" type="button" class="danger">Borrar dispositivos locales y lecturas</button>

  <script>
    const $ = (id) => document.getElementById(id);
    const msg = $('msg'), dmsg = $('dmsg');
    let lots = [];

    $('f').addEventListener('submit', async (e) => {
      e.preventDefault();
      msg.textContent = 'Validando...';
      msg.className = 'msg';
      const body = {
        email: $('email').value,
        password: $('password').value,
        backendUrl: $('backendUrl').value || undefined,
      };
      try {
        const r = await fetch('/api/v1/edge/account', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const data = await r.json();
        if (r.ok) {
          msg.textContent = 'Cuenta vinculada: ' + data.email;
          msg.className = 'msg ok';
          loadAccount();
          loadDevices();
        } else {
          msg.textContent = 'Error: ' + (data.error || r.status);
          msg.className = 'msg err';
        }
      } catch (err) {
        msg.textContent = 'Error de red: ' + err;
        msg.className = 'msg err';
      }
    });

    async function loadAccount() {
      try {
        const r = await fetch('/api/v1/edge/account');
        const data = await r.json();
        const acct = $('acct');
        if (r.ok && data.configured) {
          acct.textContent = 'Cuenta vinculada: ' + data.email +
            (data.backendUrl ? ' (' + data.backendUrl + ')' : '');
          acct.className = 'msg ok';
          if (data.email) $('email').value = data.email;
          if (data.backendUrl) $('backendUrl').value = data.backendUrl;
        } else {
          acct.textContent = 'No hay ninguna cuenta vinculada todavia.';
          acct.className = 'msg muted';
        }
      } catch (e) {
        $('acct').textContent = '';
      }
    }

    function lotOptions(selected) {
      const opts = ['<option value="">- sin asignar -</option>'];
      for (const lot of lots) {
        const label = `#${lot.id} · ${lot.lotName || ''} (${lot.coffeeType || ''})`;
        const sel = String(lot.id) === String(selected) ? ' selected' : '';
        opts.push(`<option value="${lot.id}"${sel}>${label}</option>`);
      }
      return opts.join('');
    }

    async function loadLots() {
      try {
        const r = await fetch('/api/v1/edge/lots');
        const data = await r.json();
        lots = r.ok ? (data.lots || []) : [];
        if (!r.ok) dmsg.textContent = 'Lotes: ' + (data.error || r.status);
      } catch (e) {
        lots = [];
      }
    }

    async function loadDevices() {
      dmsg.textContent = '';
      dmsg.className = 'msg';
      await loadLots();
      const r = await fetch('/api/v1/edge/devices');
      const data = await r.json();
      const tbody = $('devices').querySelector('tbody');
      tbody.innerHTML = '';
      for (const d of (data.devices || [])) {
        const tr = document.createElement('tr');
        const seen = d.lastSeenAt ? new Date(d.lastSeenAt).toLocaleString() : '-';
        const pill = d.assigned
          ? '<span class="pill ok">asignado</span>'
          : '<span class="pill pend">pendiente</span>';
        const stateNote = d.assigned
          ? `Lote actual: #${d.lotId}`
          : 'Falta asignarle un lote para que sincronice con el backend';
        const actionLabel = d.assigned ? 'Guardar cambio de lote' : 'Asignar lote';
        tr.innerHTML =
          `<td>${d.deviceId}<br>${pill}<span class="device-note">${stateNote}</span></td>` +
          `<td>${seen}</td>` +
          `<td>${d.readingCount}</td>` +
          `<td><div class="row-actions">` +
          `<select>${lotOptions(d.lotId)}</select>` +
          `<button type="button">${actionLabel}</button></div></td>`;
        const select = tr.querySelector('select');
        tr.querySelector('button').addEventListener('click', () => assign(d.deviceId, select.value));
        tbody.appendChild(tr);
      }
      if (!(data.devices || []).length) {
        tbody.innerHTML = '<tr><td colspan="4" class="muted">Aun no se anuncia ningun dispositivo.</td></tr>';
      }
    }

    async function assign(deviceId, lotId) {
      if (!lotId) {
        dmsg.textContent = 'Elige un lote primero.';
        dmsg.className = 'msg err';
        return;
      }
      const r = await fetch(`/api/v1/edge/devices/${encodeURIComponent(deviceId)}/assign`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lotId }),
      });
      const data = await r.json();
      if (r.ok) {
        dmsg.textContent = `${deviceId} quedo asignado al lote ${data.lotId}.`;
        dmsg.className = 'msg ok';
        loadDevices();
      } else {
        dmsg.textContent = 'Error: ' + (data.error || r.status);
        dmsg.className = 'msg err';
      }
    }

    $('reset').addEventListener('click', async () => {
      if (!confirm('¿Borrar todos los dispositivos IoT, sus lecturas y sus umbrales locales? La cuenta vinculada se mantiene.')) return;
      const r = await fetch('/api/v1/edge/devices/reset', { method: 'POST' });
      const data = await r.json();
      dmsg.textContent = r.ok ? `Borrados ${data.devicesDeleted} dispositivo(s).` : ('Error: ' + (data.error || r.status));
      dmsg.className = 'msg ' + (r.ok ? 'ok' : 'err');
      loadDevices();
    });

    $('refresh').addEventListener('click', loadDevices);
    loadAccount();
    loadDevices();
  </script>
</body>
</html>"""


@onboarding_api.route("/onboarding", methods=["GET"])
def onboarding_page():
    return Response(ONBOARDING_HTML, mimetype="text/html")
