# Disclaimer

**This is not a medical device. Nothing here has been reviewed, cleared, or approved by any regulatory body (FDA, MDR, or otherwise).**

## What this project actually is

Software for capturing and analyzing PPG (photoplethysmography) signals from a consumer fitness sensor (Polar Verity Sense), and for estimating blood pressure trends from that data. It was built to track blood pressure instability in a family member with Multiple System Atrophy (MSA), calibrated against a home oscillometric cuff.

It is a personal engineering project, not a clinical product. Use it the same way you'd use any other piece of DIY health software you found on GitHub: read the code, understand what it does, and decide for yourself whether you trust it.

## Specific things you should know before using this

Blood pressure estimates from PPG are not as accurate as a cuff. Even state-of-the-art deep learning models trained on large clinical datasets have roughly 2x the error of an arterial line, and several times the error of a properly-used oscillometric cuff. PPG waveform morphology correlates with BP; it does not measure it directly. Treat any number this software produces as a trend indicator, not an absolute reading.

Calibration is per-person and per-device. A model calibrated on one person's PPG-to-cuff pairs does not transfer to another person without recalibration. If you use this with your own hardware, you need your own calibration data.

This has not been validated in a clinical trial. The calibration and validation work behind this project is n=1, sometimes n=2. That is enough to be useful for personal tracking. It is not enough to be a basis for anyone's medical decisions.

Do not use this to make treatment decisions. Don't adjust medication, call an ambulance, or skip a doctor's visit based on a number this software gives you. If you or someone you're tracking feels unwell, use a real medical device and talk to a clinician.

The BLE protocol for the blood pressure cuff (Omron) was reverse-engineered, not obtained from the manufacturer. It is not guaranteed to be correct, complete, or stable across firmware versions. A parsing bug could silently give you a wrong number that looks plausible.

Software has bugs. This project has already had at least one silent data-loss bug in the wild, a sensor stream failing to start without raising a visible error. Assume there are more that haven't been found yet. Don't build anything safety-critical on top of this without your own review and testing.

## Who this is for

People comfortable reading source code, running things themselves, and treating the output with appropriate skepticism — the same audience as Nightscout, OpenAPS, and similar DIY health-tracking projects. If that's not you, this probably isn't the right tool for you yet.

## No warranty

This software is provided under the MIT License, which includes a standard "AS IS" warranty disclaimer — see `LICENSE`. That's a legal statement; this document is the plain-language version of the same point: nobody is guaranteeing this works correctly, and nobody is liable if it doesn't.

If you're building on this for your own DIY project, that's exactly what it's for. Just go in with your eyes open.
