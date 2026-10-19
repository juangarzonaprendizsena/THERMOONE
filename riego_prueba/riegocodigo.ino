// Prueba: sensores + relés + Bluetooth (ESP32 clásico)
// Librerías: "DHT sensor library" y "Adafruit Unified Sensor" (Adafruit)
// Placa: ESP32 Dev Module | Monitor serie: 9600

#include <BluetoothSerial.h>
#include <DHT.h>

BluetoothSerial BT;
DHT dht(4, DHT11);

const int PIN_BOMBA = 25;     // relé 1
const int PIN_FAN = 26;       // relé 2
const int PIN_LUZ = 27;       // relé 3
const int PIN_SUELO = 34;     // AO del YL-69
const int PIN_SUELO_VCC = 33; // VCC del YL-69 (solo se enciende al medir)

bool bomba = false, fan = false, luz = false;
unsigned long ultimo = 0;
unsigned long bombaInicio = 0;

void aplicar() {
  // Los relés se activan en LOW
  digitalWrite(PIN_BOMBA, bomba ? LOW : HIGH);
  digitalWrite(PIN_FAN, fan ? LOW : HIGH);
  digitalWrite(PIN_LUZ, luz ? LOW : HIGH);
}

void comando(String c) {
  c.trim();
  c.toUpperCase();
  if (c == "B1") { bomba = true; bombaInicio = millis(); }
  else if (c == "B0") bomba = false;
  else if (c == "F1") fan = true;
  else if (c == "F0") fan = false;
  else if (c == "L1") luz = true;
  else if (c == "L0") luz = false;
  aplicar();
  ultimo = 0;  // manda el estado enseguida, sin esperar 2 s
}

int leerSuelo() {
  digitalWrite(PIN_SUELO_VCC, HIGH);
  delay(100);
  int v = analogRead(PIN_SUELO);
  digitalWrite(PIN_SUELO_VCC, LOW);
  return v;
}

void setup() {
  Serial.begin(9600);
  // Primero HIGH para que los relés no se activen al arrancar
  digitalWrite(PIN_BOMBA, HIGH);
  digitalWrite(PIN_FAN, HIGH);
  digitalWrite(PIN_LUZ, HIGH);
  pinMode(PIN_BOMBA, OUTPUT);
  pinMode(PIN_FAN, OUTPUT);
  pinMode(PIN_LUZ, OUTPUT);
  pinMode(PIN_SUELO_VCC, OUTPUT);
  digitalWrite(PIN_SUELO_VCC, LOW);
  dht.begin();
  BT.begin("RiegoESP32");
  Serial.println("Listo. Comandos: B1 B0 F1 F0 L1 L0");
}

void loop() {
  if (Serial.available()) comando(Serial.readStringUntil('\n'));
  if (BT.available()) comando(BT.readStringUntil('\n'));

  // Seguridad: la bomba nunca pasa de 30 s encendida
  if (bomba && millis() - bombaInicio > 30000) {
    bomba = false;
    aplicar();
  }

  if (millis() - ultimo >= 2000) {
    ultimo = millis();
    int suelo = leerSuelo();
    float t = dht.readTemperature();
    float h = dht.readHumidity();
    if (isnan(t)) t = -1;
    if (isnan(h)) h = -1;

    String json = "{\"suelo\":" + String(suelo) +
                  ",\"temp\":" + String(t, 1) +
                  ",\"hum\":" + String(h, 0) +
                  ",\"bomba\":" + String(bomba) +
                  ",\"fan\":" + String(fan) +
                  ",\"luz\":" + String(luz) + "}";
    Serial.println(json);
    BT.println(json);
  }
}
