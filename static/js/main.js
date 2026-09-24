document.addEventListener("DOMContentLoaded", () => {
    // ── API Architecture ────────────────────────────────────────────────────────
    // CLOUD_API_BASE: Where the web app is hosted (for Chirix fetch, data conversion, XML export)
    const CLOUD_API_BASE = window.location.origin.startsWith("http") ? window.location.origin : "http://localhost:5000";

    // Detect if the browser is running locally on the customer PC
    const isLocalBrowser = window.location.hostname === "localhost" ||
                           window.location.hostname === "127.0.0.1" ||
                           window.location.hostname.startsWith("192.168.");

    // LOCAL_API_BASE: The customer's local Flask engine on their PC, which communicates with localhost:9000
    const LOCAL_API_BASE = isLocalBrowser ? CLOUD_API_BASE : "http://127.0.0.1:5000";

    // Company default tokens
    const COMPANY_TOKENS = {
        "RIK": "52494B54432D30303036",
        "FBT": "52494B54432D30303037"
    };

    // ── DOM refs ────────────────────────────────────────────────────────────────
    const statusDot        = document.getElementById("statusDot");
    const statusText       = document.getElementById("statusText");
    const btnRefreshStatus = document.getElementById("btnRefreshStatus");

    const btnCompanyRIK    = document.getElementById("btnCompanyRIK");
    const btnCompanyFBT    = document.getElementById("btnCompanyFBT");
    const tokenInput       = document.getElementById("tokenInput");
    const tokenHint        = document.getElementById("tokenHint");
    const tokenPreviewValue = document.getElementById("tokenPreviewValue");

    const dateFrom         = document.getElementById("dateFrom");
    const dateTo           = document.getElementById("dateTo");
    const btnFetch         = document.getElementById("btnFetch");
    const btnFetchIcon     = document.getElementById("btnFetchIcon");
    const btnFetchLabel    = document.getElementById("btnFetchLabel");
    const btnLoadSample    = document.getElementById("btnLoadSample");

    const fetchStatusBar      = document.getElementById("fetchStatusBar");
    const fetchStatusIconWrap = document.getElementById("fetchStatusIconWrap");
    const fetchStatusIcon     = document.getElementById("fetchStatusIcon");
    const fetchStatusTitle    = document.getElementById("fetchStatusTitle");
    const fetchStatusSub      = document.getElementById("fetchStatusSub");
    const btnClearFetch       = document.getElementById("btnClearFetch");

    const btnConvert        = document.getElementById("btnConvert");
    const btnPushTally      = document.getElementById("btnPushTally");
    const btnDownloadXml    = document.getElementById("btnDownloadXml");
    const btnDownloadXmlPreview = document.getElementById("btnDownloadXmlPreview");

    const invoiceSummaryBox = document.getElementById("invoiceSummaryBox");
    const statTotalCount    = document.getElementById("statTotalCount");
    const statTotalDebit    = document.getElementById("statTotalDebit");
    const statTotalCredit   = document.getElementById("statTotalCredit");
    const badgeBalanced     = document.getElementById("badgeBalanced");

    const tallyJsonCode     = document.getElementById("tallyJsonCode");
    const chirixJsonCode    = document.getElementById("chirixJsonCode");
    const btnCopyJson       = document.getElementById("btnCopyJson");
    const btnDownloadJson   = document.getElementById("btnDownloadJson");

    const resultModal       = document.getElementById("resultModal");
    const btnCloseModal     = document.getElementById("btnCloseModal");
    const btnDoneModal      = document.getElementById("btnDoneModal");
    const resCreated        = document.getElementById("resCreated");
    const resAltered        = document.getElementById("resAltered");
    const resExceptions     = document.getElementById("resExceptions");
    const rawResponseLog    = document.getElementById("rawResponseLog");

    // ── State ───────────────────────────────────────────────────────────────────
    let selectedCompany    = "RIK";
    let currentChirixData  = null;
    let convertedTallyData = null;

    // ── Date Range Initialization ──────────────────────────────────────────────
    if (dateFrom && !dateFrom.value) dateFrom.value = "2025-01-01";
    if (dateTo && !dateTo.value)     dateTo.value   = new Date().toISOString().split("T")[0];

    [dateFrom, dateTo].forEach(inp => {
        if (inp) {
            inp.addEventListener("change", () => {
                resetState();
                hideFetchStatus();
            });
        }
    });

    // ── Tally status polling (Checks Customer's Local Flask App -> Tally 9000) ──
    checkTallyStatus();
    setInterval(checkTallyStatus, 5000);
    if (btnRefreshStatus) btnRefreshStatus.addEventListener("click", checkTallyStatus);

    async function checkTallyStatus() {
        try {
            const controller = new AbortController();
            const timeoutId = setTimeout(() => controller.abort(), 2500);
            const res = await fetch(`${LOCAL_API_BASE}/api/status`, {
                signal: controller.signal
            });
            clearTimeout(timeoutId);
            const data = await res.json();
            if (data.connected) {
                statusDot.className  = "status-indicator-dot pulse-green";
                statusText.innerText = "Online (Local Tally :9000)";
            } else {
                statusDot.className  = "status-indicator-dot pulse-yellow";
                statusText.innerText = "Local Engine OK · Open Tally";
            }
        } catch {
            statusDot.className  = "status-indicator-dot pulse-red";
            statusText.innerText = isLocalBrowser ? "TallyPrime Disconnected" : "Local Engine Offline (Port 5000)";
        }
    }

    // ── Company & Token selector ────────────────────────────────────────────────
    function getRawToken() {
        return tokenInput ? tokenInput.value.trim() : "";
    }

    function updateTokenPreview() {
        const raw = getRawToken();
        const masked = raw.length > 6
            ? raw.substring(0, 6) + "···" + raw.substring(raw.length - 4)
            : (raw || "(empty)");
        if (tokenPreviewValue) {
            tokenPreviewValue.textContent = masked;
        }
    }

    function switchCompany(company) {
        selectedCompany = company;
        [btnCompanyRIK, btnCompanyFBT].forEach(btn => {
            if (btn) btn.classList.toggle("active", btn.dataset.company === company);
        });
        if (tokenHint) tokenHint.textContent = `Token for ${company}`;
        if (tokenInput && COMPANY_TOKENS[company]) {
            tokenInput.value = COMPANY_TOKENS[company];
        }
        updateTokenPreview();
        resetState();
        hideFetchStatus();
    }

    if (btnCompanyRIK) btnCompanyRIK.addEventListener("click", () => switchCompany("RIK"));
    if (btnCompanyFBT) btnCompanyFBT.addEventListener("click", () => switchCompany("FBT"));
    if (tokenInput)    tokenInput.addEventListener("input", updateTokenPreview);
    updateTokenPreview(); // init

    // ── Toggle token visibility ─────────────────────────────────────────────────
    document.querySelectorAll(".input-toggle-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const inp = document.getElementById(btn.dataset.target);
            if (!inp) return;
            const show = inp.type === "password";
            inp.type = show ? "text" : "password";
            const icon = btn.querySelector("i");
            if (icon) icon.className = show ? "fa-solid fa-eye-slash" : "fa-solid fa-eye";
        });
    });

    // ── Date helper: yyyy-mm-dd → dd/mm/yyyy ───────────────────────────────────
    function toChirixDate(iso) {
        if (!iso) return "";
        const [y, m, d] = iso.split("-");
        return `${d}/${m}/${y}`;
    }

    // ── Fetch from Chirix ───────────────────────────────────────────────────────
    if (btnFetch) btnFetch.addEventListener("click", fetchFromChirix);

    async function fetchFromChirix() {
        const tokenVal  = getRawToken();
        const fromValue = dateFrom ? dateFrom.value : "";
        const toValue   = dateTo   ? dateTo.value   : "";

        // Client-side validation
        if (!tokenVal) {
            shakeInput(tokenInput, `Please enter the Chirix-Auth-Token for ${selectedCompany}`);
            return;
        }
        if (!fromValue || !toValue) {
            alert("Please select both From Date and To Date.");
            return;
        }
        if (fromValue > toValue) {
            alert("From Date cannot be after To Date.");
            return;
        }

        setFetchLoading(true);
        hideFetchStatus();
        resetState();

        const dateFromChirix = toChirixDate(fromValue);
        const dateToChirix   = toChirixDate(toValue);

        try {
            const res = await fetch(`${CLOUD_API_BASE}/api/fetch`, {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    api_key:   tokenVal,          // Raw token string e.g. 52494B54432D30303036
                    date_from: dateFromChirix,
                    date_to:   dateToChirix
                })
            });

            const data = await res.json();

            if (!data.success) {
                showFetchStatus("error", "Fetch Failed", data.error || "Unknown error from Chirix");
                return;
            }

            const invoices = data.invoices || [];
            if (invoices.length === 0) {
                const hint = selectedCompany === "FBT"
                    ? "FBT invoices exist for June 2026, July 2026 & August 2026."
                    : "RIK invoices exist for Jan–Dec 2025 and June–August 2026.";
                showFetchStatus("warning", "No Invoices Found in Selected Period",
                    `${selectedCompany}: 0 records for ${dateFromChirix} → ${dateToChirix}. (${hint})`);
                return;
            }

            currentChirixData = invoices;
            if (chirixJsonCode) {
                chirixJsonCode.innerText = JSON.stringify(
                    { success: true, records: data.records, data: invoices }, null, 4
                );
            }

            showFetchStatus("success",
                `${data.records} Invoice${data.records !== 1 ? "s" : ""} Fetched`,
                `${selectedCompany} · ${dateFromChirix} → ${dateToChirix}`
            );

            if (btnConvert) btnConvert.disabled = false;
            performConversion();

        } catch (err) {
            showFetchStatus("error", "Network Error", err.message);
        } finally {
            setFetchLoading(false);
        }
    }

    // ── Load Sample Demo Data ────────────────────────────────────────────────────
    if (btnLoadSample) {
        btnLoadSample.addEventListener("click", () => {
            const sampleInvoices = [
                {
                    "InvNo": "RIKTN/26-27/138",
                    "InvDt": "06/08/2026",
                    "BuyerGstin": "33AADFF6762B1ZS",
                    "BuyerLglNm": "FIREBALL TECHNOLOGIES",
                    "BuyerAddr1": "SF NO 523/2, 1st Floor, BUSHIDO TOWER,",
                    "BuyerAddr2": "UDAYAMPALAYAM ROAD, NAVA INDIA, AVINASHI ROAD,",
                    "BuyerAddr3": "SOWRIPALAYAM VILLAGE, PEELAMEDU, COIMBATORE - 641028",
                    "BuyerLoc":  "Coimbatore",
                    "BuyerStcd": "33",
                    "TotAssVal": 79464.0,
                    "TotIgstVal": 0,
                    "TotCgstVal": 7146.76,
                    "TotSgstVal": 7146.76,
                    "TotDiscount": 0,
                    "OthChrg": 0,
                    "FreightChrg": 0,
                    "RndOffAmt": 0.48,
                    "TotInvVal": 93758.0,
                    "ItemList": [
                        {
                            "SiNo": 1,
                            "PrdDesc": "Sales @ GST 18%",
                            "HsnCd": "94036000",
                            "Unit": "NOS",
                            "Qty": 3,
                            "UnitPrice": 26488.0,
                            "AssAmt": 79464.0,
                            "GstRt": 18.0,
                            "CgstAmt": 7146.76,
                            "SgstAmt": 7146.76,
                            "IgstAmt": 0,
                            "TotAmt": 93757.52
                        }
                    ]
                }
            ];

            currentChirixData = sampleInvoices;
            if (chirixJsonCode) {
                chirixJsonCode.innerText = JSON.stringify(
                    { success: true, records: sampleInvoices.length, data: sampleInvoices }, null, 4
                );
            }

            showFetchStatus("success",
                `${sampleInvoices.length} Sample Demo Invoice Loaded`,
                "Sample invoice · Chirix e-Invoice format"
            );

            if (btnConvert) btnConvert.disabled = false;
            performConversion();
        });
    }

    // ── Fetch UI helpers ────────────────────────────────────────────────────────
    function setFetchLoading(on) {
        if (!btnFetch) return;
        btnFetch.disabled = on;
        if (btnFetchIcon)  btnFetchIcon.className  = on ? "fa-solid fa-spinner fa-spin" : "fa-solid fa-cloud-arrow-down";
        if (btnFetchLabel) btnFetchLabel.innerText  = on ? "Fetching…" : "Fetch Invoices from Chirix";
    }

    function showFetchStatus(type, title, sub) {
        if (!fetchStatusBar) return;
        fetchStatusBar.classList.remove("hidden");
        const map = {
            success: { icon: "fa-solid fa-circle-check",       cls: "success" },
            warning: { icon: "fa-solid fa-triangle-exclamation", cls: "warning" },
            error:   { icon: "fa-solid fa-circle-xmark",       cls: "error"   }
        };
        const info = map[type] || map.success;
        if (fetchStatusIconWrap) fetchStatusIconWrap.className = `fetch-status-icon ${info.cls}`;
        if (fetchStatusIcon)     fetchStatusIcon.className = info.icon;
        if (fetchStatusTitle)    fetchStatusTitle.innerText = title;
        if (fetchStatusSub)      fetchStatusSub.innerText   = sub;
    }

    function hideFetchStatus() {
        if (fetchStatusBar) fetchStatusBar.classList.add("hidden");
    }

    function resetState() {
        currentChirixData  = null;
        convertedTallyData = null;
        if (invoiceSummaryBox) invoiceSummaryBox.classList.add("hidden");
        if (btnConvert)     btnConvert.disabled     = true;
        if (btnPushTally)   btnPushTally.disabled   = true;
        if (btnDownloadXml) btnDownloadXml.disabled = true;
        if (tallyJsonCode)  tallyJsonCode.innerText  = "// Converted Tally JSON will appear here…";
        if (chirixJsonCode) chirixJsonCode.innerText = "// Raw Chirix API response will appear here…";
    }

    if (btnClearFetch) btnClearFetch.addEventListener("click", () => {
        hideFetchStatus();
        resetState();
    });

    // ── Validate & Convert ──────────────────────────────────────────────────────
    if (btnConvert) btnConvert.addEventListener("click", performConversion);

    async function performConversion() {
        if (!currentChirixData) return;
        if (btnConvert) {
            btnConvert.disabled = true;
            btnConvert.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Validating…';
        }
        try {
            const res  = await fetch(`${CLOUD_API_BASE}/api/convert`, {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify(currentChirixData)
            });
            const data = await res.json();
            if (!data.success) { alert("Conversion Error: " + data.error); return; }

            convertedTallyData = data.tally_json;
            if (tallyJsonCode) tallyJsonCode.innerText = JSON.stringify(convertedTallyData, null, 4);

            let totalDebit = 0, totalCredit = 0, allBalanced = true;
            (data.invoices_meta || []).forEach(m => {
                totalDebit  += m.grand_total  || 0;
                totalCredit += m.credit_total || 0;
                if (!m.balanced) allBalanced = false;
            });

            if (statTotalCount) statTotalCount.innerText = data.total_count;
            if (statTotalDebit)  statTotalDebit.innerText  = "₹" + totalDebit.toLocaleString("en-IN", { minimumFractionDigits: 2 });
            if (statTotalCredit) statTotalCredit.innerText = "₹" + totalCredit.toLocaleString("en-IN", { minimumFractionDigits: 2 });

            if (badgeBalanced) {
                badgeBalanced.className = allBalanced ? "badge badge-success" : "badge badge-danger";
                badgeBalanced.innerText = allBalanced ? "Debit = Credit Validated" : "Accounting Imbalance";
            }
            if (invoiceSummaryBox) invoiceSummaryBox.classList.remove("hidden");
            if (btnPushTally)      btnPushTally.disabled   = false;
            if (btnDownloadXml)    btnDownloadXml.disabled = false;
        } catch (err) {
            alert("Conversion failed: " + err.message);
        } finally {
            if (btnConvert) {
                btnConvert.disabled = false;
                btnConvert.innerHTML = '<i class="fa-solid fa-wand-magic-sparkles"></i> Validate &amp; Convert';
            }
        }
    }

    // ── Push to Customer's Local TallyPrime (via Local Flask App :5000) ─────────
    if (btnPushTally) {
        btnPushTally.addEventListener("click", async () => {
            if (!currentChirixData) return;
            btnPushTally.disabled = true;
            btnPushTally.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Pushing to Local Tally…';
            try {
                const res  = await fetch(`${LOCAL_API_BASE}/api/push`, {
                    method:  "POST",
                    headers: { "Content-Type": "application/json" },
                    body:    JSON.stringify(currentChirixData)
                });
                const data = await res.json();
                if (!data.success) { alert("Push Error: " + data.error); return; }

                const vM = data.voucher_metrics || { created: 0, altered: 0, exceptions: 0 };
                const mM = data.master_metrics  || { created: 0, altered: 0, exceptions: 0 };
                if (resCreated)    resCreated.innerText    = vM.created;
                if (resAltered)    resAltered.innerText    = mM.created + mM.altered;
                if (resExceptions) resExceptions.innerText = vM.exceptions + mM.exceptions;

                let log  = "=== MASTER SYNC ===\n" + (data.master_xml_response  || "N/A").trim();
                    log += "\n\n=== VOUCHER PUSH ===\n" + (data.voucher_xml_response || "N/A").trim();
                if (rawResponseLog) rawResponseLog.innerText = log;
                if (resultModal)    resultModal.classList.remove("hidden");
            } catch (err) {
                const msg = isLocalBrowser
                    ? `Push failed: ${err.message}\nMake sure TallyPrime is open on Port 9000.`
                    : `Cannot reach Local Flask App at ${LOCAL_API_BASE}.\n\n` +
                      `To push directly into Tally on your computer:\n` +
                      `1. Open a terminal on your PC and run:\n   python app.py\n` +
                      `2. Ensure TallyPrime is running.\n\n` +
                      `Alternatively, click 'Download Tally XML' to import into Tally manually (Alt + O).`;
                alert(msg);
            } finally {
                btnPushTally.disabled = false;
                btnPushTally.innerHTML = '<i class="fa-solid fa-paper-plane"></i> Push to Local Tally (Port 9000)';
            }
        });
    }

    // ── Download Tally XML for Manual Import (Alt + O in Tally) ─────────────────
    async function downloadTallyXml() {
        if (!currentChirixData) {
            alert("Please fetch and validate invoices first.");
            return;
        }
        try {
            const res = await fetch(`${CLOUD_API_BASE}/api/export-xml`, {
                method:  "POST",
                headers: { "Content-Type": "application/json" },
                body:    JSON.stringify(currentChirixData)
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const xmlBlob = await res.blob();
            const url  = URL.createObjectURL(xmlBlob);
            const a    = Object.assign(document.createElement("a"), {
                href: url,
                download: `tally_vouchers_${selectedCompany}.xml`
            });
            a.click();
            URL.revokeObjectURL(url);
        } catch (err) {
            alert("XML Export failed: " + err.message);
        }
    }

    if (btnDownloadXml)        btnDownloadXml.addEventListener("click", downloadTallyXml);
    if (btnDownloadXmlPreview) btnDownloadXmlPreview.addEventListener("click", downloadTallyXml);

    // ── Modal ───────────────────────────────────────────────────────────────────
    [btnCloseModal, btnDoneModal].forEach(b => {
        if (b) b.addEventListener("click", () => resultModal.classList.add("hidden"));
    });

    // ── Tabs ────────────────────────────────────────────────────────────────────
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach(b  => b.classList.remove("active"));
            document.querySelectorAll(".tab-pane").forEach(p => p.classList.add("hidden"));
            btn.classList.add("active");
            const t = document.getElementById(btn.dataset.target);
            if (t) t.classList.remove("hidden");
        });
    });

    // ── Copy / Download ─────────────────────────────────────────────────────────
    if (btnCopyJson) {
        btnCopyJson.addEventListener("click", () => {
            if (!convertedTallyData) return;
            navigator.clipboard.writeText(JSON.stringify(convertedTallyData, null, 4));
            btnCopyJson.innerHTML = '<i class="fa-solid fa-check text-success"></i> Copied!';
            setTimeout(() => { btnCopyJson.innerHTML = '<i class="fa-regular fa-copy"></i> Copy JSON'; }, 2000);
        });
    }
    if (btnDownloadJson) {
        btnDownloadJson.addEventListener("click", () => {
            if (!convertedTallyData) return;
            const blob = new Blob([JSON.stringify(convertedTallyData, null, 4)], { type: "application/json" });
            const url  = URL.createObjectURL(blob);
            const a    = Object.assign(document.createElement("a"), { href: url, download: "tally-import.json" });
            a.click();
            URL.revokeObjectURL(url);
        });
    }

    // ── Utility ─────────────────────────────────────────────────────────────────
    function shakeInput(el, msg) {
        if (el) {
            el.classList.add("input-error");
            el.focus();
            setTimeout(() => el.classList.remove("input-error"), 2500);
        }
        if (msg) alert(msg);
    }
});
