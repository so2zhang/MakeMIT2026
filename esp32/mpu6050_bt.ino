/*
 * ESP32 MPU-6050 IMU → Bluetooth Serial
 *
 * Reads accelerometer + gyroscope at ~50 Hz and streams CSV over
 * Bluetooth Classic Serial.
 *
 * Dependencies (install via Arduino Library Manager):
 *   - "BluetoothSerial" (built-in to ESP32 Arduino core)
 *   - No external IMU library needed; raw I2C reads used directly.
 *
 * Wiring:
 *   MPU-6050 SDA → GPIO 21
 *   MPU-6050 SCL → GPIO 22
 *   MPU-6050 VCC → 3.3 V
 *   MPU-6050 GND → GND
 *   MPU-6050 AD0 → GND  (sets I2C address to 0x68 = 0b1101000)
 *
 * Compile & flash with esptool:
 *   1. Compile in Arduino IDE (or arduino-cli) to get the .bin
 *      arduino-cli compile --fqbn esp32:esp32:esp32 mpu6050_bt
 *   2. Flash:
 *      esptool.py --chip esp32 --port /dev/ttyUSB0 --baud 921600 \
 *        write_flash -z 0x1000 mpu6050_bt.ino.bin
 *
 * Data format (over BT Serial, ~50 Hz):
 *   ax,ay,az,gx,gy,gz\n
 *   where a* = accel in m/s², g* = gyro in deg/s
 */

#include <Wire.h>
#include "BluetoothSerial.h"
#include "esp_task_wdt.h"

// ── Configuration ──────────────────────────────────────────────────────────
#define MPU_ADDR        0x68          // 0b1101000
#define SAMPLE_RATE_HZ  50            // 50 Hz is cleanly achievable
#define SAMPLE_MS       (1000 / SAMPLE_RATE_HZ)
#define WDT_TIMEOUT_S   5             // watchdog: reset if loop() stalls >5 s

// MPU-6050 register map
#define REG_PWR_MGMT_1  0x6B
#define REG_SMPLRT_DIV  0x19
#define REG_CONFIG      0x1A
#define REG_GYRO_CFG    0x1B
#define REG_ACCEL_CFG   0x1C
#define REG_ACCEL_XOUT  0x3B
#define REG_WHO_AM_I    0x75

// Full-scale ranges
// Accel ±2g  → 16384 LSB/g
// Gyro  ±250°/s → 131 LSB/(°/s)
#define ACCEL_SCALE  (9.80665f / 16384.0f)   // → m/s² per LSB
#define GYRO_SCALE   (1.0f / 131.0f)          // → °/s per LSB

BluetoothSerial SerialBT;
volatile bool bt_connected = false;

void bt_callback(esp_spp_cb_event_t event, esp_spp_cb_param_t *param) {
  if (event == ESP_SPP_SRV_OPEN_EVT) {
    bt_connected = true;
    Serial.println("BT client connected");
  } else if (event == ESP_SPP_CLOSE_EVT) {
    bt_connected = false;
    Serial.println("BT client disconnected");
  }
}

// Gyro bias (deg/s), computed during calibration
float g_bias_x = 0, g_bias_y = 0, g_bias_z = 0;
// Accel bias (m/s²), only X and Y — Z retains gravity so we correct to 0,0,+g
float a_bias_x = 0, a_bias_y = 0;

// ── Helpers ────────────────────────────────────────────────────────────────
static void mpu_write(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(reg);
  Wire.write(val);
  Wire.endTransmission();
}

static bool mpu_init() {
  // Check WHO_AM_I
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_WHO_AM_I);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 1);
  if (!Wire.available()) return false;
  uint8_t who = Wire.read();
  if (who != 0x68) return false;

  mpu_write(REG_PWR_MGMT_1, 0x01);   // Wake up, use gyro X as clock ref
  delay(10);

  // Sample rate divider: output rate = 1kHz / (1 + div)
  // For 50 Hz: div = 1000/50 - 1 = 19
  mpu_write(REG_SMPLRT_DIV, 19);

  // DLPF config 3 → 44 Hz BW accel, 42 Hz gyro — good anti-aliasing at 50 Hz
  mpu_write(REG_CONFIG, 0x03);

  mpu_write(REG_GYRO_CFG,  0x00);    // ±250 °/s
  mpu_write(REG_ACCEL_CFG, 0x00);    // ±2g

  return true;
}

static void mpu_read_raw(int16_t &ax, int16_t &ay, int16_t &az,
                          int16_t &gx, int16_t &gy, int16_t &gz) {
  Wire.beginTransmission(MPU_ADDR);
  Wire.write(REG_ACCEL_XOUT);
  Wire.endTransmission(false);
  Wire.requestFrom(MPU_ADDR, 14);   // 6 accel + 2 temp + 6 gyro

  auto read16 = []() -> int16_t {
    return (int16_t)((Wire.read() << 8) | Wire.read());
  };

  ax = read16();
  ay = read16();
  az = read16();
  (void)read16();                   // discard temperature
  gx = read16();
  gy = read16();
  gz = read16();
}

// ── Setup ──────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22);               // SDA, SCL
  Wire.setClock(400000);            // 400 kHz fast mode

  SerialBT.register_callback(bt_callback);
  SerialBT.begin("ESP32_IMU");
  Serial.println("Bluetooth started as 'ESP32_IMU'");

  // Watchdog: if loop() ever blocks longer than WDT_TIMEOUT_S, reboot cleanly
  // ESP32 Arduino core v3+ changed the API to use a config struct.
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  esp_task_wdt_config_t wdt_cfg = {
    .timeout_ms     = WDT_TIMEOUT_S * 1000,
    .idle_core_mask = 0,
    .trigger_panic  = true,
  };
  esp_task_wdt_reconfigure(&wdt_cfg);
#else
  esp_task_wdt_init(WDT_TIMEOUT_S, true);
#endif
  esp_task_wdt_add(NULL);  // watch the main loop task

  while (!mpu_init()) {
    Serial.println("MPU-6050 not found, retrying...");
    delay(500);
  }
  Serial.println("MPU-6050 initialised");

  // ── Gyro + accel bias calibration ────────────────────────────────────────
  // Keep the IMU still during this (~1.5 s)
  Serial.println("Calibrating — keep IMU still...");
  const int N = 500;
  double sg_x=0, sg_y=0, sg_z=0;
  double sa_x=0, sa_y=0;
  for (int i = 0; i < N; i++) {
    int16_t ax_r, ay_r, az_r, gx_r, gy_r, gz_r;
    mpu_read_raw(ax_r, ay_r, az_r, gx_r, gy_r, gz_r);
    sg_x += gx_r * GYRO_SCALE;
    sg_y += gy_r * GYRO_SCALE;
    sg_z += gz_r * GYRO_SCALE;
    sa_x += ax_r * ACCEL_SCALE;
    sa_y += ay_r * ACCEL_SCALE;
    delay(3);   // ~333 Hz during cal, well above 50 Hz
  }
  g_bias_x = sg_x / N;
  g_bias_y = sg_y / N;
  g_bias_z = sg_z / N;
  a_bias_x = sa_x / N;
  a_bias_y = sa_y / N;
  // Note: do NOT bias Z accel — gravity lives there at rest.

  Serial.printf("Gyro bias:  %.4f  %.4f  %.4f deg/s\n", g_bias_x, g_bias_y, g_bias_z);
  Serial.printf("Accel bias: %.4f  %.4f  (Z uncorrected) m/s²\n", a_bias_x, a_bias_y);
  Serial.println("Calibration done.");
}

// ── Loop ───────────────────────────────────────────────────────────────────
void loop() {
  esp_task_wdt_reset();   // feed the watchdog every iteration

  static uint32_t last_ms = 0;
  uint32_t now = millis();

  if (now - last_ms < SAMPLE_MS) {
    delay(1);   // yield to BT stack / RTOS scheduler — critical
    return;
  }
  last_ms = now;

  int16_t ax_r, ay_r, az_r, gx_r, gy_r, gz_r;
  mpu_read_raw(ax_r, ay_r, az_r, gx_r, gy_r, gz_r);

  float ax = ax_r * ACCEL_SCALE - a_bias_x;
  float ay = ay_r * ACCEL_SCALE - a_bias_y;
  float az = az_r * ACCEL_SCALE;
  float gx = gx_r * GYRO_SCALE  - g_bias_x;
  float gy = gy_r * GYRO_SCALE  - g_bias_y;
  float gz = gz_r * GYRO_SCALE  - g_bias_z;

  char buf[80];
  snprintf(buf, sizeof(buf), "%.3f,%.3f,%.3f,%.3f,%.3f,%.3f\n",
           ax, ay, az, gx, gy, gz);

  if (bt_connected) {
    // Only write if the TX buffer has room — never block waiting for the host.
    // Each packet is ~40 bytes; require at least 64 free before sending.
    if (SerialBT.availableForWrite() >= 64) {
      SerialBT.print(buf);
    }
    // If buffer is full we silently drop this sample rather than stalling.
  }

  // Always mirror to USB serial regardless of BT state
  Serial.print(buf);
}
