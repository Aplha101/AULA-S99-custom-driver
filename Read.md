# AULA S99 Custom Driver

An open-source custom driver and configuration tool for the **AULA S99** mechanical keyboard. This project provides cross-platform/custom support for managing RGB lighting in Music mode.

#### Why was this needed?
RGB drivers like
 *   **OpenRGB:** [Official Website](https://openrgb.org) | [GitHub Repository](https://github.com)
*   **SignalRGB:** [Official Website](https://signalrgb.com)

Do not yet support the S99 and the manufacturer driver isnt upto standards.

#### Does this only support S99?
yes. And only the S99 [although i am not sure about the S99 pro as i do not own it]

---

## Features

- **Music Visualizer:** A better adaptation to the official music mode provided by the manufacturer driver.
- **Live color switching:** Colors switch according to the majority of color on your screen.
- **Sound Sensitivity Adjustment:** Adjust to your prefrences Bass , Highs and Mids
---

## Prerequisites

Before building or using the application, make sure you have the following installed on your system:

- **Python 3.11.9 or higher**
Download Python from [here](https://www.python.org/downloads/).


## Installation & Setup
open cmd or terminal..
1. **Clone the Repository:**
   ```bash
   git clone https://github.com/Aplha101/AULA-S99-custom-driver.git

   cd AULA-S99-custom-driver
   ```

2. **Run the build.exe**
   Run the build.exe which will build the application (should take about 2-3 minutes)

---
## How to Use
After build.exe is done building , close the terminal.

Go to dist>AULA_S99.exe and run it.

It should open a screen with controls.

Press start on the bottom and play music.
### How to match screen color
Click on Calibrate and the middle button for color selector , and continue to add the basic colors (the keyboard will show which color should be chosen)

this is to ensure the default colors or the closest colors it can match the screen.

click done and the lights should react to the screen
## License

This project is open-source under the [MIT License](LICENSE).