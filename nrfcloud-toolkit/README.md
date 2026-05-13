# nRF Cloud Device Manager

GUI tool for provisioning nRF devices to nRF Cloud. The tool talks to the device through AT commands, creates local certificates, writes modem credentials, onboards the device to nRF Cloud, and saves output files per device.

## Step 1: Prepare The Device Firmware

Before using this tool, flash an AT command example/application to the device.

For nRF Connect SDK, the common AT client sample is:

```text
nrf/samples/cellular/at_client
```

The device firmware must expose an AT command interface over a serial COM port. The tool needs this because it sends AT commands to:

- Read the IMEI.
- Clear the modem security tag.
- Write Amazon Root CA 1.
- Write the generated client certificate.
- Write the generated private key.
- Verify modem credentials.

## Step 2: Connect The Device

1. Connect the nRF device to the PC by USB.
2. Make sure Windows detects the serial COM port.
3. Keep the device powered and connected while provisioning.

## Step 3: Prepare nRF Cloud

1. Open nRF Cloud.
2. Create or copy an API key with permission to onboard devices.
3. Keep the API key ready for the Provision tab.

## Step 4: Setup From Source

Use this if you want to run the Python source directly.

```powershell
cd C:\path\to\nrfcloud-toolkit
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If PowerShell blocks activation scripts:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Then activate again:

```powershell
.\.venv\Scripts\Activate.ps1
```

## Step 5: Run The Tool

From source:

```powershell
python app.py
```

From exe:

```text
nRFCloudDeviceManager.exe
```

## Step 6: Provision One Device

1. Open the `Provision Device` tab.
2. Click `Refresh`.
3. Select the correct serial COM port.
4. Enter the nRF Cloud API key.
5. Check the CA fields:
   - `CA Country`: exactly 2 letters, for example `VN` or `US`
   - `Organization`
   - `CA Common`
6. Keep the default CA paths unless you want to use your own CA:
   - `ca/ca.pem`
   - `ca/ca_key.pem`
7. Keep or change the output folder:
   - `devices`
8. Keep or change the security tag:
   - default `16842753`
9. Click `Provision Device`.
10. Wait until the log shows the device is done.
11. Reboot the device if needed so it reconnects to nRF Cloud.

## Step 7: Check Generated Files

The app creates runtime folders next to `app.py` or next to the `.exe`:

```text
ca/
devices/
```

The first time the app provisions a device, it creates a local CA if both CA files are missing:

```text
ca/ca.pem
ca/ca_key.pem
```

If only one CA file exists, the app stops with an error because `ca.pem` and `ca_key.pem` must stay as a matching pair.

Each provisioned device gets its own folder:

```text
devices/
  nrf-351034927403950/
    nrf-351034927403950_16842753_client-cert.pem
    nrf-351034927403950_16842753_private-key.pem
    onboarding.csv
```

## Step 8: Check Device Position

1. Open the `Device Position` tab.
2. Enter the nRF Cloud API key.
3. Enter the device ID, for example:

```text
nrf-351034927403950
```

4. Select the history range in hours.
5. Click `Get Position`.

## Step 9: Onboard From CSV

Use this tab if you already have an onboarding CSV.

The CSV must include:

```text
deviceId,certificate
```

Then:

1. Open the `Onboard from CSV` tab.
2. Select the CSV file.
3. Enter the nRF Cloud API key.
4. Click `Load CSV`.
5. Click `Onboard All`.

## Step 10: Build EXE

Activate the virtual environment first:

```powershell
cd C:\path\to\nrfcloud-toolkit
.\.venv\Scripts\Activate.ps1
```

Build:

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm .\nRFCloudDeviceManager.spec
```

The generated executable will be:

```text
dist/nRFCloudDeviceManager.exe
```

## Step 11: Share The Tool

For a public/shared release, share only:

```text
dist/nRFCloudDeviceManager.exe
```

Do not commit or share these folders:

```text
.venv/
build/
dist/
ca/
devices/
__pycache__/
```

Each user should let the app create their own local `ca/` and `devices/` folders unless you intentionally want everyone to use the same CA.

## Notes

- Existing CA files are reused and not overwritten.
- CA subject fields only affect creation of a new CA.
- To create a new CA, delete both `ca/ca.pem` and `ca/ca_key.pem`, then run provisioning again.
- Do not delete only one CA file. The cert and key must stay paired.
