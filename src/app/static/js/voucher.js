// 전표 입력 화면(Alpine 컴포넌트). 금액은 미리보기이고 저장 시 서버가 다시 계산한다.
function voucherForm(cfg) {
  const num = (v) => {
    const n = parseFloat(String(v ?? "").replace(/,/g, ""));
    return Number.isFinite(n) ? n : 0;
  };
  const won = (n) => Math.round(n).toLocaleString("ko-KR");
  const blankLine = () => ({ item: null, unit_id: "", qty: "", unit_price: "", warehouse_id: "", memo: "" });

  return {
    date: cfg.initial.date,
    partner: cfg.initial.partner,
    memo: cfg.initial.memo,
    lines: cfg.initial.lines.length ? cfg.initial.lines : [blankLine()],
    chargesVat: cfg.chargesVat,

    // 거래처 검색
    partnerQuery: "",
    partnerResults: [],
    partnerOpen: false,
    async searchPartners() {
      const res = await fetch(`${cfg.prefix}/lookup/partners?q=${encodeURIComponent(this.partnerQuery)}`);
      this.partnerResults = res.ok ? await res.json() : [];
      this.partnerOpen = true;
    },
    pickPartner(p) {
      this.partner = p;
      this.partnerOpen = false;
      this.partnerQuery = "";
    },

    // 품목 검색(줄마다)
    itemQuery: {},
    itemResults: [],
    itemOpenIdx: null,
    async searchItems(idx) {
      const q = this.itemQuery[idx] || "";
      const res = await fetch(`${cfg.prefix}/lookup/items?q=${encodeURIComponent(q)}`);
      this.itemResults = res.ok ? await res.json() : [];
      this.itemOpenIdx = idx;
    },
    pickItem(idx, item) {
      const line = this.lines[idx];
      line.item = item;
      // 품목에 정한 매출·매입 기본 단위(없으면 재고 단위)
      const unit = item.units.find((u) => String(u.id) === String(item.default_unit_id)) || item.units[0];
      line.unit_id = String(unit.id);
      line.unit_price = String(num(item.price) * num(unit.factor));
      if (!line.qty) line.qty = "1";
      this.itemOpenIdx = null;
      this.itemQuery[idx] = "";
      if (idx === this.lines.length - 1) this.lines.push(blankLine());
    },
    changeUnit(idx) {
      const line = this.lines[idx];
      const unit = line.item.units.find((u) => String(u.id) === String(line.unit_id));
      if (unit) line.unit_price = String(num(line.item.price) * num(unit.factor));
    },
    removeLine(idx) {
      this.lines.splice(idx, 1);
      if (!this.lines.length) this.lines.push(blankLine());
    },

    // 금액
    factor(line) {
      const unit = line.item?.units.find((u) => String(u.id) === String(line.unit_id));
      return unit ? num(unit.factor) : 1;
    },
    stockQtyText(line) {
      if (!line.item || this.factor(line) === 1) return "";
      const q = num(line.qty) * this.factor(line);
      return `= ${q.toLocaleString("ko-KR")}${line.item.stock_unit}`;
    },
    supply(line) {
      return line.item ? Math.round(num(line.qty) * num(line.unit_price)) : 0;
    },
    get supplyTotal() {
      return this.lines.reduce((s, l) => s + this.supply(l), 0);
    },
    get vatTotal() {
      if (!this.chargesVat) return 0;
      const taxable = this.lines.filter((l) => l.item && !l.item.tax_exempt).reduce((s, l) => s + this.supply(l), 0);
      return Math.floor(taxable * 0.1);
    },
    won,
  };
}
