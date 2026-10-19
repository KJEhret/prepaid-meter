/*
 * Prepaid energy meter: ESP32-S3 firmware
 *
 * Part of the BEng final-year project "Prepaid Energy Monitoring for Student
 * Residences" (K. Ehret, Stellenbosch University, 2026). The firmware design
 * is described in Section 3.3 of the report.
 *
 * What it does:
 *	- Samples conditioned mains voltage and load current simultaneously with
 *	  an ADS131M02 ADC (8 kSPS per channel, read over SPI on each DRDY interrupt).
 *	- Calculates RMS voltage and current, real and apparent power, and power
 *	  factor every 8000 sample pairs, then updates used energy and prepaid balance.
 *	- Switches the load relay according to the requested state and balance.
 *	- Shows real power and remaining balance on an SSD1306 OLED every second.
 *	- Publishes telemetry over MQTT once per second and accepts load and
 *	  balance commands.
 *	- Keeps energy, balance, and requested load state in flash (NVS), saving
 *	  after commands or once per minute; restores them at startup.
 *
 * The MQTT topics and telemetry payload are listed in README.md.
 */

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <Preferences.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include "esp_timer.h"
#include <math.h>

// Wi-Fi and MQTT settings
const char* DEVICE_ID = "esp32_01";
#include "wifi_settings.h"
const int MQTT_PORT = 1883;
String TOPIC_TELEMETRY = String("meter/") + DEVICE_ID + "/telemetry";
String TOPIC_LOAD = String("meter/") + DEVICE_ID + "/load/set";
String TOPIC_BALANCE_SET = String("meter/") + DEVICE_ID + "/balance/set";
String TOPIC_BALANCE_ADD = String("meter/") + DEVICE_ID + "/balance/add";
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);
Preferences preferences;

// ESP32 pins for ADS131M02
const int MOSI_PIN = 8;
const int MISO_PIN = 9;
const int SCLK_PIN = 10;
const int ADC_DRDY = 11;
const int ADC_CS = 12;
const int ADC_RST = 13;

// ESP32 pin for load relay
const int LOAD_PIN = 14;

// OLED settings
const int OLED_SCL = 2;
const int OLED_SDA = 1;
const int OLED_WIDTH = 128;
const int OLED_HEIGHT = 64;
const uint8_t OLED_ADDRESS = 0x3C;
Adafruit_SSD1306 oled(OLED_WIDTH, OLED_HEIGHT, &Wire, -1);
bool oledReady = false;

// Measurement settings
const int SAMPLE_RATE = 8000;
const int WINDOW_SIZE = 8000;
const double NO_LOAD_CURRENT_A = 0.005; // below 5 mA the load is considered off

// Phase correction selected experimentally (report Section 4.3). 
const int PHASE_SHIFT_SAMPLES = 3;

// Delay voltage by three samples for real-power calculation only.
double phaseDelayBuffer[PHASE_SHIFT_SAMPLES] = {0.0};
int phaseDelayIndex = 0;
int phaseDelayCount = 0;

// ADS131M02 settings
const double ADC_VREF = 1.2;
const int VOLTAGE_GAIN = 2;
const int CURRENT_GAIN = 4;

// ADC volts per code: 1 LSB = VREF / (gain x 2^23)
const double VOLTAGE_VOLTS_PER_CODE = ADC_VREF / (pow(2, 23) * VOLTAGE_GAIN);
const double CURRENT_VOLTS_PER_CODE = ADC_VREF / (pow(2, 23) * CURRENT_GAIN);

// Calibrated scale factors from ADC input to voltage and current (report Section 4.2)
const double VOLTAGE_SCALE = 597;
const double CURRENT_SCALE = 189.48;

// Latest calculated measurements
double vrms = 0.0;
double irms = 0.0;
double realPower = 0.0;
double apparentPower = 0.0;
double powerFactor = 0.0;
double energyKWh = 0.0;
double balanceKWh = 10.0;

// Load state
bool loadRequested = true;
bool loadOn = true;

// Measurement accumulation
double sumV = 0.0;
double sumI = 0.0;
double sumV2 = 0.0;
double sumI2 = 0.0;
double sumVI = 0.0;
double sumPowerV = 0.0;
double sumPowerI = 0.0;
int sampleCount = 0;
int powerSampleCount = 0;

// Each falling edge of DRDY indicates that a new ADS131M02 conversion is available.
volatile uint32_t samplesWaiting = 0;

// Timing and persistent state
int64_t lastMeasurementTime = 0;
uint32_t lastPublish = 0;
uint32_t lastMQTTAttempt = 0;
uint32_t lastSave = 0;
uint32_t lastWiFiAttempt = 0;
bool wifiConnectedReported = false;
bool stateChanged = false;

/**
 * @brief ADC data-ready (DRDY) interrupt; counts new sample pairs for loop() to read.
 * @note  IRAM_ATTR keeps it in internal RAM so it can run while flash is busy.
 */
void IRAM_ATTR dataReadyInterrupt() {
	samplesWaiting++;
}

/**
 * @brief Saves energy, balance and the requested load state to flash (NVS),
 *        so they survive a restart (see loadState()).
 * @note  Called after every command, and from loop() at most once per minute.
 */
void saveState() {
	preferences.putDouble("energy", energyKWh);
	preferences.putDouble("balance", balanceKWh);
	preferences.putBool("load", loadRequested);
	stateChanged = false;
}

/**
 * @brief Restores energy, balance and the requested load state from flash (NVS)
 *        at startup.
 * @note  Default values: 0 kWh energy, a 10 kWh balance and the load requested on.
 */
void loadState() {
	preferences.begin("meter", false);
	energyKWh = preferences.getDouble("energy", 0.0);
	balanceKWh = preferences.getDouble("balance", 10.0);
	loadRequested = preferences.getBool("load", true);
}

/**
 * @brief Shows real power and the remaining prepaid balance on OLED.
 * @note  Does nothing if no OLED detected at startup.
 */
void updateOLED() {
	if (!oledReady) return;
	oled.clearDisplay();
	oled.setTextColor(SSD1306_WHITE);
	oled.setTextSize(1);
	oled.setCursor(0, 0);
	oled.println("POWER");
	oled.setTextSize(2);
	oled.setCursor(0, 10);
	oled.print(realPower, 1);
	oled.print(" W");
	oled.setTextSize(1);
	oled.setCursor(0, 36);
	oled.println("UNITS");
	oled.setTextSize(2);
	oled.setCursor(0, 46);
	oled.print(balanceKWh, 3);
	oled.print(" kWh");
	oled.display();
}

/**
 * @brief Switches the relay: the load is on only when requested and the
 *        prepaid balance > 0 kwh.
 */
void updateLoad() {
	loadOn = loadRequested && balanceKWh > 0.0;
	if (loadOn) {
		digitalWrite(LOAD_PIN, HIGH);
	} else {
		digitalWrite(LOAD_PIN, LOW);
	}
}

/**
 * @brief  Converts a 24-bit two's complement ADC value to a signed 32 bit integer.
 * @param  a  Most significant byte
 * @param  b  Middle byte
 * @param  c  Least significant byte
 * @return Signed sample value from ADC
 */
int32_t signed24(uint8_t a, uint8_t b, uint8_t c) {
	uint32_t value = ((uint32_t)a << 16) | ((uint32_t)b << 8) | c;

	if (value & 0x800000) {
		value |= 0xFF000000;
	}
	return (int32_t)value;
}

/**
 * @brief Configures the ADS131M02: both channels on, 8 kSPS (OSR 512,
 *        high-resolution mode), gain 2 for voltage (CH0) and 4 for current (CH1).
 * @note  Writes the CLOCK and GAIN1 registers with one WREG command.
 */
void configureADC() {
	SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE1));
	digitalWrite(ADC_CS, LOW);
	// WREG command starting at CLOCK register 0x03.
	SPI.transfer(0x61);
	SPI.transfer(0x81);
	SPI.transfer(0x00);
	// CLOCK = 0x030A CH0 and CH1 enabled OSR = 512 High resolution mode 
	// 8.192MHz/2/512 = 8 ksample/s
	SPI.transfer(0x03);
	SPI.transfer(0x0A);
	SPI.transfer(0x00);
	// GAIN1 = 0x0021 CH0 gain = 2 CH1 gain = 4
	SPI.transfer(0x00);
	SPI.transfer(0x21);
	SPI.transfer(0x00);
	// Pad to full 4 word frame (CRC is unused)
	SPI.transfer(0x00);
	SPI.transfer(0x00);
	SPI.transfer(0x00);
	digitalWrite(ADC_CS, HIGH);
	SPI.endTransaction();
}

/**
 * @brief Reads one ADS131M02 frame over SPI (STATUS, CH0, CH1 and CRC words).
 * @param ch0  Output: voltage channel sample in ADC codes
 * @param ch1  Output: current channel sample in ADC codes
 */
void readADC(int32_t &ch0, int32_t &ch1) {

	uint8_t data[12];
	SPI.beginTransaction(SPISettings(4000000, MSBFIRST, SPI_MODE1));
	digitalWrite(ADC_CS, LOW);
    // read 12 bytes (4 words) from the ADC: STATUS, CH0, CH1, CRC
	for (int i = 0; i < 12; i++) {
		data[i] = SPI.transfer(0x00); 
	}
	digitalWrite(ADC_CS, HIGH);
	SPI.endTransaction();

	// Convert ch0 and ch1 words from 24-bit two's complement to signed 32 bit integers
	ch0 = signed24(data[3], data[4], data[5]);
	ch1 = signed24(data[6], data[7], data[8]);
}

/**
 * @brief Reads and discards two frames so that measurement starts with fresh
 *        samples after the ADC is configured.
 */
void clearADCFIFO() {
	int32_t dummy0;
	int32_t dummy1;
	readADC(dummy0, dummy1);
	readADC(dummy0, dummy1);
}

/**
 * @brief Clears the running sums and sample counts for the next measurement window.
 */
void resetMeasurementWindow() {
	sumV = 0.0;
	sumI = 0.0;
	sumV2 = 0.0;
	sumI2 = 0.0;
	sumVI = 0.0;
	sumPowerV = 0.0;
	sumPowerI = 0.0;
	sampleCount = 0;
	powerSampleCount = 0;
}

/**
 * @brief Calculates RMS voltage and current, real and apparent power and power
 *        factor from one window of samples, then updates energy and balance.
 * @note  DC offsets are removed from the RMS values and real power.
 */
void calculateMeasurements() {
	// Mean voltage and current
	double meanV = sumV / WINDOW_SIZE;
	double meanI = sumI / WINDOW_SIZE;
	// Mean square values with DC offset removed
	double v2 = sumV2 / WINDOW_SIZE - meanV * meanV;
	double i2 = sumI2 / WINDOW_SIZE - meanI * meanI;

	// Ensure non negative values for RMS calculations.
	if (v2 < 0.0) v2 = 0.0;
	if (i2 < 0.0) i2 = 0.0;

	// RMS
	vrms = sqrt(v2);
	irms = sqrt(i2);

	// Real power P = mean(v x i)
	if (powerSampleCount > 0) {
		double meanPowerV = sumPowerV / powerSampleCount;
		double meanPowerI = sumPowerI / powerSampleCount;
		realPower = sumVI / powerSampleCount - meanPowerV * meanPowerI;
	} else realPower = 0.0;

	// Apparent power
	apparentPower = vrms * irms;

	// Power factor
	if (irms < NO_LOAD_CURRENT_A) {
		irms = 0.0;
		realPower = 0.0;
		apparentPower = 0.0;
		powerFactor = 0.0;
	} else if (apparentPower > 0.000001) {
		powerFactor = realPower / apparentPower;
		powerFactor = constrain(powerFactor, -1.0, 1.0);
	} else powerFactor = 0.0;

	// Elapsed time since the previous window.
	int64_t now = esp_timer_get_time();
	double seconds = (now - lastMeasurementTime) / 1000000.0;
	lastMeasurementTime = now;

	// Energy and prepaid balance
	if (realPower > 0.0) {
		double usedKWh = realPower * seconds / 3600000.0; // convert W to kWh
		energyKWh += usedKWh;
		balanceKWh -= usedKWh;
        // Ensure balance does not go negative and update load relay if negative.
		if (balanceKWh <= 0.0) {
			balanceKWh = 0.0;
			updateLoad();
		}
		stateChanged = true;
	}
	resetMeasurementWindow();
}

/**
 * @brief Handles commands received over MQTT.
 * @param topic    Topic message arrived on (load/set, balance/set, balance/add)
 * @param payload  Message bytes
 * @param length   Number of bytes in payload
 * @note  Each command updates relay and OLED and saves the state immediately.
 */
void mqttCallback(char* topic, byte* payload, unsigned int length) {
	String message;
	for (unsigned int i = 0; i < length; i++) message += (char)payload[i];
	message.trim();
	// Load on/off command
	if (strcmp(topic, TOPIC_LOAD.c_str()) == 0) {
		loadRequested = message == "1" || message.equalsIgnoreCase("ON");
		updateLoad();
		updateOLED();
		saveState();
	}
	// Set prepaid balance
	else if (strcmp(topic, TOPIC_BALANCE_SET.c_str()) == 0) {
		balanceKWh = message.toDouble();
		if (balanceKWh < 0.0) balanceKWh = 0.0;
		updateLoad();
		updateOLED();
		saveState();
	}
	// Add prepaid balance
	else if (strcmp(topic, TOPIC_BALANCE_ADD.c_str()) == 0) {
		balanceKWh += message.toDouble();
		if (balanceKWh < 0.0) balanceKWh = 0.0;
		updateLoad();
		updateOLED();
		saveState();
	}
}

/**
 * @brief Connects to the MQTT broker and subscribes to the command topics.
 * @note  Only tries when WiFi is connected at most once every 5s.
 */
void connectMQTT() {
	if (WiFi.status() != WL_CONNECTED) return;
	if (mqtt.connected()) return;
	if (millis() - lastMQTTAttempt < 5000) return;
	lastMQTTAttempt = millis();
	if (mqtt.connect(DEVICE_ID)) {
		mqtt.subscribe(TOPIC_LOAD.c_str());
		mqtt.subscribe(TOPIC_BALANCE_SET.c_str());
		mqtt.subscribe(TOPIC_BALANCE_ADD.c_str());
		Serial.print("MQTT connected as ");
		Serial.println(DEVICE_ID);
	}
}

/**
 * @brief Prints the latest measurements, balance and load state as JSON to
 *        serial monitor and publishes them to MQTT when connected.
 */
void publishTelemetry() {
	char payload[300];
	int loadValue = 0;
	if (loadOn) loadValue = 1;
	snprintf(payload, sizeof(payload),
		"{"
		"\"vrms\":%.3f,"
		"\"irms\":%.3f,"
		"\"p_w\":%.3f,"
		"\"s_va\":%.3f,"
		"\"pf\":%.4f,"
		"\"energy_kwh\":%.6f,"
		"\"balance_kwh\":%.6f,"
		"\"load\":%d"
		"}",
		vrms, irms, realPower, apparentPower, powerFactor,
		energyKWh, balanceKWh, loadValue);
	Serial.println(payload);
	if (mqtt.connected()) {
		mqtt.publish(TOPIC_TELEMETRY.c_str(), payload);
	}
}

/**
 * @brief Restores the saved state, starts the OLED,
 *        resets and configures the ADC, starts WiFi and MQTT in the background
 *        and enables the DRDY interrupt.
 */
void setup() {
	Serial.begin(115200);
	delay(500);
	Serial.println();
	Serial.print("Starting meter: ");
	Serial.println(DEVICE_ID);

	// Load control
	pinMode(LOAD_PIN, OUTPUT);
	digitalWrite(LOAD_PIN, LOW);
	loadState();
	updateLoad();

	// OLED
	Wire.begin(OLED_SDA, OLED_SCL);
	Wire.setClock(400000);
	if (oled.begin(SSD1306_SWITCHCAPVCC, OLED_ADDRESS)) {
		oledReady = true;
		oled.clearDisplay();
		oled.setTextColor(SSD1306_WHITE);
		oled.setTextSize(1);
		oled.setCursor(0, 0);
		oled.println("Power meter starting...");
		oled.display();
	} else Serial.println("SSD1306 OLED not detected");

	// ADS131M02 pins
	pinMode(ADC_CS, OUTPUT);
	pinMode(ADC_RST, OUTPUT);
	pinMode(ADC_DRDY, INPUT);
	digitalWrite(ADC_CS, HIGH);
	digitalWrite(ADC_RST, HIGH);

	// SPI
	SPI.begin(SCLK_PIN, MISO_PIN, MOSI_PIN, ADC_CS);

	// Reset ADS131M02
	Serial.println("Resetting ADS131M02...");
	digitalWrite(ADC_RST, LOW);
	delay(10);
	digitalWrite(ADC_RST, HIGH);
	delay(100);

	// Configure ADC
	configureADC();
	Serial.println("ADS131M02 configured");

	// WiFi
	WiFi.mode(WIFI_STA);
	WiFi.setAutoReconnect(true);
	WiFi.begin(WIFI_NAME, WIFI_PASSWORD);
	Serial.println("Connecting to WiFi in background");
	lastWiFiAttempt = millis();

	// MQTT
	mqtt.setServer(MQTT_BROKER, MQTT_PORT);
	mqtt.setCallback(mqttCallback); // Set callback for incoming messages
	lastMQTTAttempt = millis() - 5000;
	connectMQTT();

	// Prepare ADC
	clearADCFIFO();
	resetMeasurementWindow();

	// Ignore DRDY events that occurred during startup.
	noInterrupts();
	samplesWaiting = 0;
	interrupts();

	// Initialise timing
	lastMeasurementTime = esp_timer_get_time();

	// Enable DRDY interrupt
	attachInterrupt(digitalPinToInterrupt(ADC_DRDY), dataReadyInterrupt, FALLING);

	// Initial OLED values
	updateOLED();
	Serial.println("Power meter started");
}

/**
 * @brief Main loop: reads and accumulates waiting ADC samples, completes a
 *        measurement window every 8000 sample pairs, keeps WiFi and MQTT
 *        connected, publishes telemetry once per second and saves changed
 *        state at most once per minute.
 */
void loop() {
	// ADC acquisition
	while (samplesWaiting > 0) {
		noInterrupts();
		samplesWaiting--;
		interrupts();
		int32_t voltageCode;
		int32_t currentCode;
		readADC(voltageCode, currentCode);
		// Convert voltage ADC code
		double voltage = voltageCode * VOLTAGE_VOLTS_PER_CODE * VOLTAGE_SCALE;
		// Convert current ADC code
		double current = currentCode * CURRENT_VOLTS_PER_CODE * CURRENT_SCALE;
		// Accumulate samples
		sumV += voltage;
		sumI += current;
		sumV2 += voltage * voltage;
		sumI2 += current * current;

		// Apply phase correction only to instantaneous real-power calculation.
		double delayedVoltage = phaseDelayBuffer[phaseDelayIndex];
		phaseDelayBuffer[phaseDelayIndex] = voltage;

		if (++phaseDelayIndex >= PHASE_SHIFT_SAMPLES) {
			phaseDelayIndex = 0;
		}
		// Accumulate real power only after the phase delay buffer is filled.
		if (phaseDelayCount < PHASE_SHIFT_SAMPLES) {
			phaseDelayCount++;
		} else {
			sumVI += delayedVoltage * current;
			sumPowerV += delayedVoltage;
			sumPowerI += current;
			powerSampleCount++;
		}
		sampleCount++;

		// Complete 1s measurement window
		if (sampleCount >= WINDOW_SIZE) {
			calculateMeasurements();
			updateOLED();
			// The OLED update takes about 23ms so DRDY events ignored.
			noInterrupts();
			samplesWaiting = 0;
			interrupts();
		}
	}
	if (WiFi.status() == WL_CONNECTED) {
		if (!wifiConnectedReported) {
			Serial.print("WiFi connected: ");
			Serial.println(WiFi.localIP());
			wifiConnectedReported = true;
		}
	} else {
		wifiConnectedReported = false;
		if (millis() - lastWiFiAttempt >= 10000) {
			lastWiFiAttempt = millis();
			WiFi.reconnect();
		}
	}
	// Maintain MQTT connection
	connectMQTT();
	mqtt.loop();
	// Publish telemetry once per second
	if (millis() - lastPublish >= 1000) {
		lastPublish = millis();
		publishTelemetry();
	}
	// Save changed state at most once per minute
	if (stateChanged && millis() - lastSave >= 60000) {
		lastSave = millis();
		saveState();
	}
}
