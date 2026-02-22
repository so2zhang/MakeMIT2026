// Flex sensor pins (ADC1)
const int pointerPin = 32;
const int middlePin  = 33;
const int ringPin    = 34;
const int pinkyPin   = 35;

// Capacitive touch pin (must be touch-capable, e.g., GPIO 4)
const int thumbTouchPin = 15;

void setup() {
  Serial.begin(115200);

  // Set ADC resolution (ESP32 default is 12-bit: 0–4095)
  analogReadResolution(12);

  Serial.println("ESP32 Hand Sensor Readings");
  Serial.println("--------------------------------");
}

void loop() {

  // Read flex sensors
  int pointerValue = analogRead(pointerPin);
  int middleValue  = analogRead(middlePin);
  int ringValue    = analogRead(ringPin);
  int pinkyValue   = analogRead(pinkyPin);

  // Convert to voltage (0–3.3V range)
  float pointerVoltage = pointerValue * (3.3 / 4095.0);
  float middleVoltage  = middleValue  * (3.3 / 4095.0);
  float ringVoltage    = ringValue    * (3.3 / 4095.0);
  float pinkyVoltage   = pinkyValue   * (3.3 / 4095.0);

  // Read capacitive touch value
  int thumbTouchValue = touchRead(thumbTouchPin);

  // Print results
  Serial.println("----- Hand Data -----");

  Serial.print("Pointer Finger: ");
  Serial.print(pointerVoltage);
  Serial.println(" V");

  Serial.print("Middle Finger:  ");
  Serial.print(middleVoltage);
  Serial.println(" V");

  Serial.print("Ring Finger:    ");
  Serial.print(ringVoltage);
  Serial.println(" V");

  Serial.print("Pinky Finger:   ");
  Serial.print(pinkyVoltage);
  Serial.println(" V");

  Serial.print("Thumb Touch Value: ");
  Serial.println(thumbTouchValue);

  Serial.println();

  delay(500);
}