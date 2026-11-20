"""
Waveshare 1.69\" LCD Module — SKU 24382
Resolution: 240×280, driver: ST7789V2, 4-wire SPI.

Pin map (BCM / physical) matches Waveshare wiki:
  https://www.waveshare.net/wiki/1.69inch_LCD_Module
"""

# Display geometry
WIDTH = 240
HEIGHT = 280

# SPI (SPI0, CE0) — use board.SPI() on Pi; MOSI/SCLK/CE0 are fixed by header.
SPI_BAUDRATE = 50_000_000  # Lower to 32_000_000 if you see glitches.

# GPIO (BCM numbers) — must match your wiring
PIN_DC = 25   # Data/Command — wiki: Pi phys pin 22
PIN_RST = 27  # Reset — wiki: Pi phys pin 13
# Backlight: any free GPIO works (driver only sets on/off). Default wiki uses
# GPIO18 (phys 12), but that is I2S BCLK for the microphones, and GPIO12/13 are
# the hardware PWM pins driving the tilt and pan servos.
PIN_BL = 24   # GPIO24 (phys 18). Rewire LCD BL to this BCM pin.

# ST7789 internal RAM is 240×320; this panel shows rows 20–299 of it, so the
# image must start at row 20. With 0 the picture sits 20 px too high and the
# bottom 20 rows show uninitialised RAM (random pixels).
X_OFFSET = 0
Y_OFFSET = 20

# 0 = portrait (240 wide × 280 tall). Try 90, 180, 270 if text is sideways.
ROTATION = 0

