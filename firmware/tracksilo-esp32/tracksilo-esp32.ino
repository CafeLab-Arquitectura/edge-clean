/*
 * TrackSilo - Firmware del sensor ESP32 + DHT22 para CafeLab.
 *
 * Firmware GENÉRICO: se flashea igual en todos los dispositivos, sin hornear
 * credenciales. El device_id se deriva de la MAC y la api_key se obtiene en
 * runtime (auto-enrollment) y se guarda en NVS. NO hay que reflashear por
 * dispositivo ni configurar el lote aquí: el lote se asigna desde la web
 * /onboarding del edge.
 *
 * Flujo:
 *   1. WiFiManager: si no hay WiFi guardado, abre el AP "TrackSilo-Setup" con
 *      portal cautivo para que el usuario ingrese la red del cafe.
 *   2. Resuelve el edge por IP fija (EDGE_FALLBACK_IP) o, si está vacía, por
 *      mDNS (<hostname>.local). En la práctica el mDNS del ESP32 suele fallar,
 *      así que la IP fija es el camino confiable.
 *   3. Announce (phone-home): POST /api/v1/iam/devices/announce {deviceId:MAC}
 *      -> el edge devuelve la api_key, que se guarda en NVS (Preferences).
 *   4. Loop: lee el DHT22 y hace POST /api/v1/edge/readings con la api_key.
 *      La respuesta trae humidityAlert y temperatureAlert; enciende el pin de
 *      humedad (18) y el de temperatura (19) de forma independiente. Si recibe
 *      401 (p.ej. el edge fue reseteado), borra la key y se vuelve a anunciar.
 *
 * Librerias (Library Manager):
 *   - WiFiManager        (tzapu)
 *   - DHT sensor library (Adafruit) + Adafruit Unified Sensor
 *   - ArduinoJson v7     (bblanchon)
 *   ESPmDNS / WiFi / HTTPClient / Preferences vienen con el core de ESP32.
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <DHT.h>

// ===================== CONFIG (igual para todos los dispositivos) =====================
// Credenciales WiFi directas para conexión automática (Hotspot)
static const char* WIFI_SSID        = "U202320083";
static const char* WIFI_PASSWORD    = "kaletito";

// device_id = esp32-<mac> automáticamente. Para SIMULAR otro IoT en la misma
// placa, pon aquí un id fijo (vacío = usar la MAC).
static const char* DEVICE_ID_OVERRIDE = "";
static const char* DEVICE_PREFIX      = "esp32-";

// Descubrimiento del edge (en cascada): 1) servicio mDNS _cafelab._tcp,
// 2) hostname mDNS, 3) IP fija de respaldo.
static const char* EDGE_SERVICE     = "cafelab";        // => _cafelab._tcp (avahi del Pi)
static const char* EDGE_HOST        = "raspberrypi";    // hostname mDNS (sin ".local")
static const char* EDGE_FALLBACK_IP = "172.20.10.3";    // IP de la MacBook en el Hotspot del iPhone
static const uint16_t EDGE_PORT     = 5000;

static const uint8_t DHT_PIN      = 15;               // dato del DHT22
static const uint8_t DHT_KIND     = DHT22;
// Dos actuadores independientes: el edge responde humidityAlert y temperatureAlert
// (cada uno true cuando la variable esta fuera de rango: > max o < min).
static const uint8_t HUMIDITY_ACTUATOR_PIN    = 18;  // p.ej. deshumidificador
static const uint8_t TEMPERATURE_ACTUATOR_PIN = 19;  // p.ej. enfriador/calefactor

static const unsigned long READ_INTERVAL_MS = 60000; // 60 segundos
// =====================================================================================

DHT dht(DHT_PIN, DHT_KIND);
Preferences prefs;
IPAddress edgeIp;
String deviceId;
String apiKey;
unsigned long lastReadAt = 0;

String computeDeviceId() {
  if (strlen(DEVICE_ID_OVERRIDE) > 0) {
    return String(DEVICE_ID_OVERRIDE);   // id fijo para simular otro IoT
  }
  String mac = WiFi.macAddress();   // formato AA:BB:CC:DD:EE:FF
  mac.replace(":", "");
  mac.toLowerCase();
  return String(DEVICE_PREFIX) + mac;
}

IPAddress resolveEdge() {
  // 1) Descubrimiento por SERVICIO mDNS (_cafelab._tcp). Es lo más fiable en
  //    ESP32 y no depende de adivinar el hostname; el Pi lo anuncia vía Avahi.
  for (int attempt = 0; attempt < 3; attempt++) {
    int n = MDNS.queryService(EDGE_SERVICE, "tcp");
    if (n > 0) {
      IPAddress ip = MDNS.address(0);
      Serial.printf("[mdns] edge por servicio '%s': %s:%u (%s)\n",
                    EDGE_SERVICE, ip.toString().c_str(), MDNS.port(0),
                    MDNS.hostname(0).c_str());
      return ip;
    }
    delay(500);
  }

  // 2) Resolución por hostname (sin ".local"), con reintentos.
  for (int i = 0; i < 5; i++) {
    IPAddress ip = MDNS.queryHost(EDGE_HOST);
    if (ip != IPAddress(0, 0, 0, 0)) {
      Serial.printf("[mdns] edge por host '%s': %s\n", EDGE_HOST, ip.toString().c_str());
      return ip;
    }
    delay(500);
  }

  // 3) Respaldo: IP fija (deja EDGE_FALLBACK_IP vacío para forzar solo-mDNS).
  if (strlen(EDGE_FALLBACK_IP) > 0) {
    IPAddress ip;
    if (ip.fromString(EDGE_FALLBACK_IP)) {
      Serial.printf("[mdns] mDNS falló; usando IP fija %s\n", EDGE_FALLBACK_IP);
      return ip;
    }
  }
  return IPAddress(0, 0, 0, 0);
}

String edgeUrl(const String& path) {
  return "http://" + edgeIp.toString() + ":" + String(EDGE_PORT) + path;
}

// Phone-home: se anuncia al edge y obtiene (y persiste) su api_key.
bool announce() {
  if (edgeIp == IPAddress(0, 0, 0, 0)) {
    edgeIp = resolveEdge();
    if (edgeIp == IPAddress(0, 0, 0, 0)) {
      Serial.println("[announce] no se pudo resolver el edge");
      return false;
    }
  }

  JsonDocument body;
  body["deviceId"] = deviceId;
  String payload;
  serializeJson(body, payload);

  HTTPClient http;
  http.begin(edgeUrl("/api/v1/iam/devices/announce"));
  http.addHeader("Content-Type", "application/json");
  int status = http.POST(payload);
  bool ok = false;

  if (status == 200) {
    JsonDocument resp;
    if (deserializeJson(resp, http.getString()) == DeserializationError::Ok) {
      apiKey = String((const char*)(resp["api_key"] | ""));
      if (apiKey.length() > 0) {
        prefs.putString("apiKey", apiKey);
        bool assigned = resp["assigned"] | false;
        Serial.printf("[announce] ok; %s\n",
          assigned ? "con lote" : "PENDIENTE: asigna un lote en /onboarding");
        ok = true;
      }
    }
  } else {
    Serial.printf("[announce] fallo %d: %s\n", status, http.getString().c_str());
  }
  http.end();
  return ok;
}

void applyActuators(bool humidityAlert, bool temperatureAlert) {
  digitalWrite(HUMIDITY_ACTUATOR_PIN, humidityAlert ? HIGH : LOW);
  digitalWrite(TEMPERATURE_ACTUATOR_PIN, temperatureAlert ? HIGH : LOW);
}

void sendReading(float temperature, float humidity) {
  if (edgeIp == IPAddress(0, 0, 0, 0)) {
    edgeIp = resolveEdge();
    if (edgeIp == IPAddress(0, 0, 0, 0)) {
      Serial.println("[edge] no se pudo resolver el edge (mDNS/fallback)");
      return;
    }
    Serial.print("[edge] encontrado en ");
    Serial.println(edgeIp);
  }

  if (apiKey.length() == 0 && !announce()) {
    return;  // sin api_key no se puede autenticar
  }

  JsonDocument body;
  body["deviceId"] = deviceId;
  body["temperature"] = temperature;
  body["humidity"] = humidity;

  time_t nowSecs = time(nullptr);
  struct tm timeinfo;
  char timeStr[32];
  if (gmtime_r(&nowSecs, &timeinfo) && nowSecs > 100000) {
    strftime(timeStr, sizeof(timeStr), "%Y-%m-%dT%H:%M:%SZ", &timeinfo);
    body["measured_at"] = timeStr;
  } else {
    snprintf(timeStr, sizeof(timeStr), "%lu", millis());
    body["measured_at"] = timeStr;
  }
  String payload;
  serializeJson(body, payload);

  HTTPClient http;
  http.begin(edgeUrl("/api/v1/edge/readings"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", apiKey);

  int status = http.POST(payload);
  if (status > 0) {
    String response = http.getString();
    Serial.printf("[edge] %d %s\n", status, response.c_str());

    if (status == 200 || status == 201) {
      JsonDocument resp;
      if (deserializeJson(resp, response) == DeserializationError::Ok) {
        bool humidityAlert = resp["humidityAlert"] | false;
        bool temperatureAlert = resp["temperatureAlert"] | false;
        applyActuators(humidityAlert, temperatureAlert);
        Serial.printf("[actuador] humedad=%s temperatura=%s\n",
                      humidityAlert ? "ON" : "OFF",
                      temperatureAlert ? "ON" : "OFF");
      }
    } else if (status == 401) {
      // El edge no reconoce la key (p.ej. fue reseteado): re-anunciarse.
      Serial.println("[edge] 401: re-anunciando para obtener nueva api_key");
      apiKey = "";
      prefs.remove("apiKey");
      announce();
    }
  } else {
    Serial.printf("[edge] POST fallo: %s\n", http.errorToString(status).c_str());
    edgeIp = IPAddress(0, 0, 0, 0);  // forzar re-resolucion la proxima vez
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  pinMode(HUMIDITY_ACTUATOR_PIN, OUTPUT);
  pinMode(TEMPERATURE_ACTUATOR_PIN, OUTPUT);
  digitalWrite(HUMIDITY_ACTUATOR_PIN, LOW);
  digitalWrite(TEMPERATURE_ACTUATOR_PIN, LOW);
  dht.begin();

  // Conexión directa y automática al Hotspot
  Serial.printf("[wifi] conectando directamente a '%s'...\n", WIFI_SSID);
  WiFi.disconnect(true); // Limpiar conexiones previas
  delay(500);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Evita ahorro de energía en Wi-Fi para no perder conexión con iPhone
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();
  // El Hotspot de iPhone suele tardar hasta 20-25s en negociar DHCP
  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 25000) {
    delay(500);
    Serial.print(".");
  }

  // Si no se pudo conectar en 25s, resetea estado Wi-Fi y abre portal cautivo como respaldo
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\n[wifi] no se pudo conectar al Hotspot; abriendo portal 'TrackSilo-Setup' de respaldo...");
    WiFi.disconnect(true);
    delay(1000);
    WiFiManager wm;
    wm.setConfigPortalTimeout(180); // 3 minutos para configurar
    if (!wm.autoConnect("TrackSilo-Setup")) {
      Serial.println("[wifi] fallo de conexion; reiniciando...");
      delay(3000);
      ESP.restart();
    }
  }
  Serial.print("\n[wifi] conectado exitosamente, IP asignada: ");
  Serial.println(WiFi.localIP());

  configTime(0, 0, "pool.ntp.org", "time.nist.gov");

  deviceId = computeDeviceId();
  Serial.print("[id] deviceId = ");
  Serial.println(deviceId);

  prefs.begin("tracksilo", false);
  apiKey = prefs.getString("apiKey", "");

  if (!MDNS.begin(deviceId.c_str())) {
    Serial.println("[mdns] no se pudo iniciar mDNS local");
  }
  edgeIp = resolveEdge();
  if (edgeIp != IPAddress(0, 0, 0, 0)) {
    Serial.print("[edge] resuelto en ");
    Serial.println(edgeIp);
  }

  // Si no tenemos api_key guardada, anunciarse para obtenerla.
  if (apiKey.length() == 0) {
    announce();
  } else {
    Serial.println("[id] api_key recuperada de NVS");
  }
}

void loop() {
  unsigned long now = millis();
  if (now - lastReadAt < READ_INTERVAL_MS) {
    return;
  }
  lastReadAt = now;

  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();
  if (isnan(humidity) || isnan(temperature)) {
    Serial.println("[dht] lectura invalida");
    return;
  }

  Serial.printf("[dht] T=%.1fC H=%.1f%%\n", temperature, humidity);
  sendReading(temperature, humidity);
}
