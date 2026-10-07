#define CLK 2
#define DT 3
#define SW 4
#include <GyverEncoder.h>
Encoder enc(CLK, DT, SW);
bool holdFlag = false;   // Флаг: было ли начато удержание
bool clickFlag = false;  // Флаг: был ли начат клик
bool lastState = HIGH;   // Для отслеживания физического поднятия кнопки
void setup() {
  Serial.begin(115200); 
  enc.setType(TYPE2);
  enc.setFastTimeout(40);
}
void loop() {
  enc.tick();
  // 1. Физическое состояние кнопки сейчас
  bool currentState = enc.isHold(); 
  // --- ОБЫЧНОЕ ВРАЩЕНИЕ ---
  if (enc.isRight()) Serial.println(F("Right"));
  if (enc.isLeft())  Serial.println(F("Left"));
  // --- ВРАЩЕНИЕ С ЗАЖАТОЙ КНОПКОЙ ---
  if (enc.isRightH()) {
    Serial.println(F("Hold Right"));
    holdFlag = true; // Запоминаем, что это режим удержания
  }
  if (enc.isLeftH()) {
    Serial.println(F("Hold Left"));
    holdFlag = true;
  }
  // --- СОБЫТИЯ НАЖАТИЯ ---
  if (enc.isClick()) {
    Serial.println(F("Click"));
    clickFlag = true;
  }
  if (enc.isHolded()) {
    Serial.println(F("Hold"));
    holdFlag = true;
  }
  if (enc.isDouble()) {
    Serial.println(F("DoubleClick"));
    holdFlag = true;
  }
  // --- ЛОГИКА ОТПУСКАНИЯ (РУЧНАЯ) ---
  if (lastState && !currentState) {
    // Кнопка только что была отпущена (физически)
    if (holdFlag) {
      Serial.println(F("Hold Release"));
      holdFlag = false;
      clickFlag = false; // Сбрасываем всё
    } 
    else if (clickFlag) {
      Serial.println(F("Click Release"));
      clickFlag = false;
    }
  }
  // Обновляем состояние для следующего цикла
  lastState = currentState; 
}