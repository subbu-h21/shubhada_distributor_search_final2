import React, { useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from './ui/dialog';
import { OrderAPI, screenshotUrl } from '../lib/api';
import { Loader2, CheckCircle2, ShoppingCart, XCircle } from 'lucide-react';
import { toast } from 'sonner';

/** Modal launched from a ResultCard's "ADD TO ORDER" button.
 *  Collects: product (readonly), supplier, qty, mobile, patient, advance.
 *  On Save → POST /api/order/place → automation runs on shubhadahealth.com.
 */
const AddToOrderSheet = ({ open, onOpenChange, defaults }) => {
  const [product, setProduct] = useState('');
  const [supplier, setSupplier] = useState('');
  const [qty, setQty] = useState('1');
  const [mobile, setMobile] = useState('');
  const [patient, setPatient] = useState('');
  const [advance, setAdvance] = useState('0');
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [result, setResult] = useState(null); // { ok, error, screenshots, steps }

  React.useEffect(() => {
    if (open && defaults) {
      setProduct(defaults.product || '');
      setSupplier(defaults.supplier || '');
      setQty(String(defaults.qty || 1));
      setMobile('');
      setPatient('');
      setAdvance('0');
      setResult(null);
      setElapsed(0);
    }
  }, [open, defaults]);

  const canSave = product.trim() && patient.trim() && parseInt(qty, 10) > 0;

  const onSave = async () => {
    if (!canSave) { toast.error('Product, patient name and qty are required'); return; }
    setBusy(true); setResult(null); setElapsed(0);
    try {
      const r = await OrderAPI.placeAndWait(
        {
          product: product.trim(),
          supplier: supplier.trim(),
          qty: parseInt(qty, 10) || 1,
          mobile: mobile.trim(),
          patient: patient.trim(),
          advance: parseFloat(advance) || 0,
        },
        { pollMs: 3000, timeoutMs: 240000, onProgress: (s) => setElapsed(s) },
      );
      if (r.ok) {
        setResult({ ok: true, ...r });
        toast.success('Order placed on Shubhada PO');
      } else {
        setResult({ ok: false, ...r });
        toast.error(r.error || 'Order failed');
      }
    } catch (e) {
      const d = e?.response?.data?.detail;
      const parsed = typeof d === 'object' ? d : { error: (typeof d === 'string' ? d : (e.message || 'Order failed')) };
      setResult({ ok: false, ...parsed });
      toast.error(parsed.error || 'Order failed');
    } finally { setBusy(false); }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md sm:max-w-lg border-emerald-600 rounded-sm p-0 gap-0" data-testid="add-to-order-dialog">
        <DialogHeader className="px-6 pt-5 pb-3 border-b border-neutral-200">
          <DialogTitle className="text-[16px] font-extrabold mono-track uppercase leading-none flex items-center gap-2">
            <ShoppingCart className="w-4 h-4 text-emerald-600" />
            ADD TO ORDER
          </DialogTitle>
          <p className="mt-1 text-[10px] text-neutral-500 mono-track-wide font-medium">
            SHUBHADA PHARMA · SHUBHADAHEALTH.COM:7007
          </p>
        </DialogHeader>

        <div className="p-6 space-y-3 max-h-[70vh] overflow-y-auto">
          {/* Product (readonly) */}
          <div>
            <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">PRODUCT</label>
            <input type="text" value={product} readOnly
              className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold bg-neutral-50 text-neutral-800"
              data-testid="ato-product" />
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">SUPPLIER</label>
              <input type="text" value={supplier} onChange={(e) => setSupplier(e.target.value)}
                placeholder="e.g. A K Pharma"
                className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold focus:border-emerald-600 outline-none"
                data-testid="ato-supplier" />
            </div>
            <div>
              <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">QTY</label>
              <input type="number" min="1" value={qty} onChange={(e) => setQty(e.target.value)}
                className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold focus:border-emerald-600 outline-none"
                data-testid="ato-qty" />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">PATIENT NAME *</label>
              <input type="text" value={patient} onChange={(e) => setPatient(e.target.value)}
                placeholder="Full name" required
                className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold focus:border-emerald-600 outline-none"
                data-testid="ato-patient" />
            </div>
            <div>
              <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">MOBILE</label>
              <input type="tel" value={mobile} onChange={(e) => setMobile(e.target.value.replace(/[^\d]/g, ''))} maxLength={10}
                placeholder="10-digit"
                className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold focus:border-emerald-600 outline-none"
                data-testid="ato-mobile" />
            </div>
          </div>

          <div>
            <label className="text-[10px] mono-track-wide text-neutral-500 font-semibold">ADVANCE AMOUNT (₹)</label>
            <input type="number" min="0" step="0.01" value={advance} onChange={(e) => setAdvance(e.target.value)}
              className="w-full h-11 px-3 mt-1 border border-neutral-300 rounded-sm text-[13px] font-semibold focus:border-emerald-600 outline-none"
              data-testid="ato-advance" />
          </div>

          {/* Actions */}
          <div className="flex items-center gap-2 pt-2">
            <button type="button" onClick={() => onOpenChange(false)} disabled={busy}
              className="h-11 px-4 border border-neutral-300 hover:border-emerald-600 rounded-sm text-[11px] mono-track-wide font-semibold press"
              data-testid="ato-cancel">
              CANCEL
            </button>
            <button type="button" onClick={onSave} disabled={busy || !canSave}
              className="flex-1 h-11 bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-[12px] mono-track-wide rounded-sm press flex items-center justify-center gap-2 disabled:opacity-50"
              data-testid="ato-save">
              {busy ? <><Loader2 className="w-4 h-4 animate-spin" /> PLACING ORDER… {elapsed > 0 && `(${elapsed}s)`}</> : <>SAVE & PLACE ON SHUBHADA PO</>}
            </button>
          </div>

          {/* Result panel */}
          {result && (
            <div className={`mt-2 border rounded-sm p-3 ${result.ok ? 'border-emerald-300 bg-emerald-50' : 'border-red-300 bg-red-50'}`}
                 data-testid="ato-result">
              <div className="flex items-center gap-2 mb-2">
                {result.ok ? (
                  <><CheckCircle2 className="w-4 h-4 text-emerald-700" />
                    <span className="text-[12px] font-bold text-emerald-800 mono-track-wide">ORDER PLACED SUCCESSFULLY</span></>
                ) : (
                  <><XCircle className="w-4 h-4 text-red-700" />
                    <span className="text-[12px] font-bold text-red-800 mono-track-wide">{result.error || 'Automation failed'}</span></>
                )}
              </div>
              {result.steps && result.steps.length > 0 && (
                <details className="text-[10px] text-neutral-600 mono-track-tight">
                  <summary className="cursor-pointer font-semibold">AUTOMATION STEPS ({result.steps.length})</summary>
                  <ol className="mt-1 pl-4 space-y-0.5 list-decimal">
                    {result.steps.map((s, i) => <li key={i}>{s}</li>)}
                  </ol>
                </details>
              )}
              {result.screenshots && result.screenshots.length > 0 && (
                <details className="mt-2 text-[10px] text-neutral-600 mono-track-tight" open={!result.ok}>
                  <summary className="cursor-pointer font-semibold">SCREENSHOTS ({result.screenshots.filter(Boolean).length})</summary>
                  <div className="mt-2 grid grid-cols-2 gap-2">
                    {result.screenshots.filter(Boolean).map((s) => (
                      <a key={s} href={screenshotUrl(s)} target="_blank" rel="noreferrer">
                        <img src={screenshotUrl(s)} alt={s} className="w-full border border-neutral-300 rounded" />
                      </a>
                    ))}
                  </div>
                </details>
              )}
            </div>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
};

export default AddToOrderSheet;
