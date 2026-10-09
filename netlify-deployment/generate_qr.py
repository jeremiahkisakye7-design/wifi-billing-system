import qrcode
import sys

# Generate QR code for the WiFi billing system
if len(sys.argv) != 2 or not sys.argv[1].startswith(('http://', 'https://')):
    raise SystemExit('Usage: python generate_qr.py https://your-site-name.netlify.app')

portal_url = sys.argv[1].rstrip('/')
qr = qrcode.QRCode(
    version=1,
    error_correction=qrcode.constants.ERROR_CORRECT_H,
    box_size=10,
    border=4,
)
qr.add_data(portal_url)
qr.make(fit=True)

# Use a high-contrast blue-on-white palette for reliable camera scanning.
img = qr.make_image(fill_color='#075bb5', back_color='#ffffff')
img.save('wifi-billing-qr.png')

print('QR Code Generated Successfully!')
print(f'📱 Scans to: {portal_url}')
print('Design: Blue modules on a white background')
print('💾 File: wifi-billing-qr.png')
print('📍 Folder: wifi billing system/')
