import qr from 'qrcode-terminal';
/** Vendor QR payload plus local terminal representation; never writes stdout directly. */
export function asciiQRCode(value) { let out=''; qr.generate(value,{small:true},text=>{out+=text;}); return out; }
