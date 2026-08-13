# NAP Drive Panel for Comma 3X

A lightweight, zero-overhead local web dashboard and dashcam viewer designed specifically for custom openpilot forks (like Flowpilot, SunnyPilot, and older Tesla hardware pedal setups). 

This script serves a fully reactive, iOS-friendly interface directly from your Comma 3/3X to any device on your local network (like an iPad, iPhone, or MacBook) without bogging down the Comma's processor. 

## ✨ Features

### 🛡 Universal Fork Compatibility (Auto-Healing)
Custom forks frequently change, rename, or drop Cap'n Proto data schemas. This backend uses **Universal Fork Introspection**. When started, it dynamically reads your car's schema, adapting to legacy `controlsState` structures or the modern openpilot 0.11+ `selfdriveState` and `_DynamicEnum` objects. If a service is missing, it heals itself and continues running without crashing.

### 📱 Flawless iOS Dashcam Viewer
Safari on iOS notoriously rejects raw openpilot `.ts` video files and requires the `moov` atom at the start of an `.mp4`. This tool bypasses Apple's strict video restrictions by utilizing the Comma's built-in `ffmpeg`.
* Instantly remuxes `.ts` to `.mp4` with `-movflags faststart`.
* Uses the `/dev/shm` RAM disk to cache video files, ensuring **0 CPU overhead** and preventing flash storage wear.
* Provides perfect video scrubbing and playback on iPads and iPhones.
* **Continuous Playback:** Automatically chains 1-minute segments together for uninterrupted drive viewing.
* **Save Clips:** 1-click download button to save `.mp4` segments directly to your device's Camera Roll or Files app.

### 🏎 Faux-3D Live Visualizer
100% of the visual processing is offloaded to your viewing device's browser, keeping the Comma's CPU free for driving.
* **Speed-Synced Road:** CSS-animated lane lines dynamically scroll based on your live `vEgo` speed.
* **True-Depth Lead Car:** The lead car physically scales and shifts on the Y-axis based on radar distance (`dRel`).
* **Threat Glow:** If closing speed is too high or Forward Collision Warning (FCW) triggers, the lead car casts a glowing red warning aura.

### 🎛 Live Controls & Telemetry
Adjust your drive without touching the Comma screen. Includes toggles for:
* Driving Personality (Chill, Standard, Aggressive)
* Follow Distance (1-7)
* Experimental Mode
* Adaptive Acceleration 
* Live readout of speed, target acceleration, active planner, and dual-lead radar telemetry.

---

## 🚀 Installation

This tool requires absolutely zero external dependencies. The entire web server, API, HTML, CSS (Glassmorphism UI), and JavaScript are bundled into a single drop-in Python file.

1. SSH into your Comma 3X.
2. Download the script directly to your `/data/` directory:


