// QR codes drawn as SVG from the bundled qrcode-generator (MIT). Works offline.
import qrcode from '../../vendor/qrcode.mjs';
import { s } from '../ui.js';

export function qrSvg(text, px = 200) {
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const n = qr.getModuleCount();
  const quiet = 3;
  let d = '';
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (qr.isDark(r, c)) d += `M${c + quiet} ${r + quiet}h1v1h-1z`;
  const size = n + quiet * 2;
  return s('svg', { viewBox: `0 0 ${size} ${size}`, width: px, height: px, role: 'img', 'aria-label': `QR code for ${text}`, class: 'qr' },
    s('rect', { width: size, height: size, fill: '#fff' }), s('path', { d, fill: '#000' }));
}
