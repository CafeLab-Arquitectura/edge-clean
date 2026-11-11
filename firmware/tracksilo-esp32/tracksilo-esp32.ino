/*
 * TrackSilo - Firmware del sensor ESP32 + DHT22 para CafeLab.
 *
 * Flujo de red probado y robusto para Hotspot iPhone / Wi-Fi local:
 *   1. Reinicio completo de radio Wi-Fi (WIFI_OFF -> WIFI_STA) sin modo de ahorro.
 *   2. Intento de conexion automatica con reintentos continuos hacia el Hotspot iPhone.
 *   3. Si no conecta tras 90s, abre el portal cautivo "TrackSilo-Setup" como respaldo.
 *   4. Announce automático con el Edge en 172.20.10.3:5000 (o mDNS).
 *   5. Loop de lecturas ambientales cada 60s con actuador independiente de humedad y temperatura.
 */

#include <WiFi.h>
#include <WiFiManager.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include <DHT.h>

// ===================== CONFIGURACIÓN RED Y EDGE =====================
// Credenciales del Hotspot iPhone
static const char* DEMO_WIFI_SSID = "U202320083";
static const char* DEMO_WIFI_PASS = "kaletito";

// IP de la MacBook en el Hotspot de iPhone (172.20.10.x)
static const char* EDGE_HOST_DEFAULT = "172.20.10.3";
static const uint16_t EDGE_PORT      = 5000;

// false = conectar directo y usar Wi-Fi guardado. true = forzar portal de configuración.
static const bool OPEN_SETUP_PORTAL  = false;

static const char* DEVICE_ID_OVERRIDE = "";
static const char* DEVICE_PREFIX      = "esp32-";

static const uint8_t DHT_PIN      = 15;               // Pin de datos DHT22
static const uint8_t DHT_KIND     = DHT22;
static const uint8_t HUMIDITY_ACTUATOR_PIN    = 18;  // LED / Actuador Humedad
static const uint8_t TEMPERATURE_ACTUATOR_PIN = 19;  // LED / Actuador Temperatura

static const unsigned long READ_INTERVAL_MS = 60000; // 60 segundos
// ====================================================================

DHT dht(DHT_PIN, DHT_KIND);
Preferences prefs;
String deviceId;
String apiKey;
String edgeHost;
unsigned long lastReadAt = 0;

String computeDeviceId() {
  if (strlen(DEVICE_ID_OVERRIDE) > 0) {
    return String(DEVICE_ID_OVERRIDE);
  }
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toLowerCase();
  return String(DEVICE_PREFIX) + mac;
}

String edgeUrl(const char* path) {
  return String("http://") + edgeHost + ":" + String(EDGE_PORT) + path;
}

void resetWifiRadio() {
  WiFi.disconnect(false);
  delay(400);
  WiFi.mode(WIFI_OFF);
  delay(200);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(WIFI_PS_NONE);
  WiFi.setAutoReconnect(true);
  delay(200);
}

void logVisibleNetworks() {
  Serial.println("\n[wifi] escaneando redes 2.4 GHz disponibles...");
  int count = WiFi.scanNetworks(false, true);
  if (count <= 0) {
    Serial.println("[wifi]   Ninguna red 2.4 GHz visible.");
    Serial.println("[wifi]   Asegurate de tener en el iPhone: Ajustes > Compartir Internet > Maximizar compatibilidad = ON");
  } else {
    for (int i = 0; i < count && i < 15; i++) {
      Serial.printf("[wifi]   Detectada red: '%s' (senal: %d dBm)\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i));
    }
  }
  WiFi.scanDelete();
}

bool connectWithSavedWifi(unsigned long timeoutMs = 90000,
                          const char* ssid = nullptr,
                          const char* pass = nullptr) {
  if (WiFi.status() == WL_CONNECTED) {
    return true;
  }

  resetWifiRadio();

  if (ssid != nullptr && strlen(ssid) > 0) {
    Serial.printf("[wifi] conectando a '%s' (hotspot iPhone, hasta %lus)...\n",
                  ssid, timeoutMs / 1000);
    WiFi.begin(ssid, pass != nullptr ? pass : "");
  } else {
    Serial.printf("[wifi] conectando con credenciales guardadas (hasta %lus)...\n",
                  timeoutMs / 1000);
    WiFi.begin();
  }

  unsigned long deadline = millis() + timeoutMs;
  unsigned long lastAttemptAt = millis();
  wl_status_t lastStatus = WL_IDLE_STATUS;

  while (millis() < deadline) {
    wl_status_t status = WiFi.status();
    if (status == WL_CONNECTED) {
      Serial.println();
      Serial.print("[wifi] conectado exitosamente, IP ESP32: ");
      Serial.println(WiFi.localIP());
      return true;
    }

    // Si falla conexion, no encuentra SSID, o queda desconectado por mas de 8s, reintenta
    if (status == WL_CONNECT_FAILED || status == WL_NO_SSID_AVAIL || (status == WL_DISCONNECTED && millis() - lastAttemptAt > 8000)) {
      if (status != lastStatus) {
        if (status == WL_NO_SSID_AVAIL) {
          Serial.println("\n[wifi] status=1: red no encontrada (verifica 'Maximizar compatibilidad' en iPhone)");
        } else {
          Serial.printf("\n[wifi] status=%d (reintentando negociacion Wi-Fi...)\n", status);
        }
        lastStatus = status;
      }
      WiFi.disconnect(false);
      delay(1000);
      lastAttemptAt = millis();
      if (ssid != nullptr && strlen(ssid) > 0) {
        WiFi.begin(ssid, pass != nullptr ? pass : "");
      } else {
        WiFi.begin();
      }
    }

    delay(500);
    Serial.print(".");
  }

  Serial.println();
  Serial.printf("[wifi] fallo conexion (status=%d)\n", WiFi.status());
  return false;
}

void setupWifiAndEdge() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  WiFi.persistent(true);

  if (edgeHost.length() == 0) {
    edgeHost = prefs.getString("edgeHost", EDGE_HOST_DEFAULT);
  }

  bool connected = false;

  if (OPEN_SETUP_PORTAL) {
    Serial.println("[wifi] OPEN_SETUP_PORTAL=true -> portal TrackSilo-Setup");
    resetWifiRadio();
    WiFiManager wm;
    wm.setBreakAfterConfig(true);
    wm.setConnectTimeout(60);
    wm.setConfigPortalTimeout(300);
    wm.resetSettings();
    connected = wm.startConfigPortal("TrackSilo-Setup");
  } else {
    // Escaneo previo para diagnosticar redes visibles en el monitor serie
    logVisibleNetworks();

    Serial.println("[wifi] 1) Intentando conectar a 'U202320083'...");
    connected = connectWithSavedWifi(45000, DEMO_WIFI_SSID, DEMO_WIFI_PASS);

    if (!connected) {
      Serial.println("[wifi] 2) Probando variante SSID 'U2023'...");
      connected = connectWithSavedWifi(45000, "U2023", DEMO_WIFI_PASS);
    }

    if (!connected) {
      Serial.println("[wifi] 3) Probando credenciales guardadas en NVS...");
      connected = connectWithSavedWifi(30000);
    }
  }

  if (!connected) {
    Serial.println("[wifi] sin conexion directa; abriendo portal 'TrackSilo-Setup' de respaldo...");
    resetWifiRadio();
    delay(1000);

    WiFiManager wm;
    wm.setBreakAfterConfig(true);
    wm.setConnectTimeout(60);
    wm.setConfigPortalTimeout(300);
    wm.setConnectRetries(2);
    wm.setSaveConnect(true);

    connected = wm.startConfigPortal("TrackSilo-Setup");
    if (!connected) {
      connected = connectWithSavedWifi(45000, DEMO_WIFI_SSID, DEMO_WIFI_PASS);
    }
  }

  if (!connected) {
    Serial.println("[wifi] no se pudo conectar; revisa: Hotspot ON, 'Maximizar compatibilidad' y clave");
    return;
  }

  Serial.print("[wifi] Listo, IP ESP32: ");
  Serial.println(WiFi.localIP());
  Serial.print("[edge] Destino: http://");
  Serial.print(edgeHost);
  Serial.print(":");
  Serial.println(EDGE_PORT);
}

bool announce() {
  JsonDocument body;
  body["deviceId"] = deviceId;
  String payload;
  serializeJson(body, payload);

  HTTPClient http;
  http.setTimeout(10000);
  http.begin(edgeUrl("/api/v1/iam/devices/announce"));
  http.addHeader("Content-Type", "application/json");

  int status = http.POST(payload);
  bool ok = false;

  if (status == 200) {
    JsonDocument resp;
    if (deserializeJson(resp, http.getString()) == DeserializationError::Ok) {
      apiKey = String((const char*)(resp["api_key"] | resp["apiKey"] | ""));
      if (apiKey.length() > 0) {
        prefs.putString("apiKey", apiKey);
        bool assigned = resp["assigned"] | false;
        Serial.printf("[announce] OK: %s (api_key guardada)\n",
                      assigned ? "dispositivo asignado" : "PENDIENTE: asigna lote en /onboarding");
        ok = true;
      }
    }
  } else if (status > 0) {
    Serial.printf("[announce] fallo HTTP %d: %s\n", status, http.getString().c_str());
  } else {
    Serial.printf("[announce] fallo de red: %s\n", http.errorToString(status).c_str());
  }

  http.end();
  return ok;
}

void applyActuators(bool humidityAlert, bool temperatureAlert) {
  digitalWrite(HUMIDITY_ACTUATOR_PIN, humidityAlert ? HIGH : LOW);
  digitalWrite(TEMPERATURE_ACTUATOR_PIN, temperatureAlert ? HIGH : LOW);
}

void sendReading(float temperature, float humidity) {
  if (apiKey.length() == 0 && !announce()) {
    return;
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
  http.setTimeout(10000);
  http.begin(edgeUrl("/api/v1/edge/readings"));
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-API-Key", apiKey);

  int status = http.POST(payload);
  if (status > 0) {
    String response = http.getString();
    Serial.printf("[edge] HTTP %d: %s\n", status, response.c_str());

    if (status == 200 || status == 201) {
      JsonDocument resp;
      if (deserializeJson(resp, response) == DeserializationError::Ok) {
        bool humidityAlert = resp["humidityAlert"] | false;
        bool temperatureAlert = resp["temperatureAlert"] | false;
        applyActuators(humidityAlert, temperatureAlert);
        Serial.printf("[actuador] Humedad=%s | Temperatura=%s\n",
                      humidityAlert ? "ON (LED 18)" : "OFF",
                      temperatureAlert ? "ON (LED 19)" : "OFF");
      }
    } else if (status == 401) {
      Serial.println("[edge] 401 Unauthorized: re-anunciando dispositivo...");
      apiKey = "";
      prefs.remove("apiKey");
      announce();
    }
  } else {
    Serial.printf("[edge] POST fallo: %s\n", http.errorToString(status).c_str());
  }
  http.end();
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\n=== TrackSilo ESP32 CafeLab ===");

  pinMode(HUMIDITY_ACTUATOR_PIN, OUTPUT);
  pinMode(TEMPERATURE_ACTUATOR_PIN, OUTPUT);
  digitalWrite(HUMIDITY_ACTUATOR_PIN, LOW);
  digitalWrite(TEMPERATURE_ACTUATOR_PIN, LOW);
  dht.begin();

  deviceId = computeDeviceId();
  prefs.begin("tracksilo", false);
  apiKey = prefs.getString("apiKey", "");
  edgeHost = prefs.getString("edgeHost", EDGE_HOST_DEFAULT);

  setupWifiAndEdge();

  Serial.print("[id] deviceId = ");
  Serial.println(deviceId);
  if (apiKey.length() > 0) {
    Serial.println("[id] apiKey cargada desde NVS");
  } else if (WiFi.status() == WL_CONNECTED) {
    announce();
  }

  configTime(0, 0, "pool.ntp.org", "time.nist.gov");
  Serial.println("[setup] listo para capturar telemetria.");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    if (!connectWithSavedWifi(45000, DEMO_WIFI_SSID, DEMO_WIFI_PASS)) {
      return;
    }
    if (apiKey.length() == 0) {
      announce();
    }
  }

  unsigned long now = millis();
  if (now - lastReadAt < READ_INTERVAL_MS) {
    delay(200);
    return;
  }
  lastReadAt = now;

  float humidity = dht.readHumidity();
  float temperature = dht.readTemperature();

  if (isnan(humidity) || isnan(temperature)) {
    Serial.println("[dht22] Error al leer del sensor DHT22 (revisa cables en pin 15)");
    return;
  }

  Serial.printf("[dht22] Temp=%.1f C | Hum=%.1f %%\n", temperature, humidity);
  sendReading(temperature, humidity);
}
