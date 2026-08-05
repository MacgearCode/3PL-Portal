/**
 * 3PL Portal RESTlet — the ONLY thing that talks to NetSuite for the portal.
 *
 * n8n (token-based auth) POSTs an {action, ...} body. READ actions run the validated
 * SuiteQL (docs/netsuite_validation.md) and return rows shaped for the app's /admin/ingest.
 * The WRITE action creates a DRAFT invoice from billing lines and returns its id.
 *
 * No app/AI/MCP involvement — pure server-to-server SuiteScript.
 *
 * Deploy: Customization > Scripting > Scripts > New, upload this file, Type=RESTlet,
 * POST function = `post`, Deploy as Released to an integration role with the needed
 * permissions (run SuiteQL, create invoices). Use the deployment's External URL from n8n.
 *
 * @NApiVersion 2.1
 * @NScriptType Restlet
 */
define(['N/query', 'N/record'], function (query, record) {

  function runSuiteQL(sql) {
    var rows = [];
    var more = true, page = 0;
    while (more) {
      var res = query.runSuiteQLPaged({ query: sql, pageSize: 1000 });
      res.pageRanges.forEach(function (r) {
        res.fetch({ index: r.index }).data.asMappedResults().forEach(function (m) { rows.push(m); });
      });
      more = false; // runSuiteQLPaged already returns all pages via pageRanges
    }
    return rows;
  }

  // group flat header/line rows (keyed by tranid id) into {header..., lines:[...]}
  function group(flat, idKey, header, line) {
    var byId = {}, order = [];
    flat.forEach(function (r) {
      var id = String(r[idKey]);
      if (!byId[id]) { byId[id] = header(r); byId[id].lines = []; order.push(id); }
      var ln = line(r); if (ln) byId[id].lines.push(ln);
    });
    return order.map(function (id) { return byId[id]; });
  }

  function truthy(v) { return v === true || v === 'true' || v === 1 || v === '1'; }

  // Per-customer stock isolation. Every read is scoped by the item's brand class. When the customer
  // is `location_scoped` (its brand also covers non-3PL / Macgear-owned stock, e.g. Mova on its
  // regular MOVA brand), we additionally filter by the dedicated 3PL location so only 3PL activity
  // comes through. When it's not location_scoped (brand is 3PL-exclusive and stock may span
  // locations, e.g. Skriva across Auckland + Christchurch), class alone is the correct isolation
  // and we must NOT filter by location. `alias` = the transactionline/inventorybalance table alias.
  function locClause(p, alias) {
    return (truthy(p.location_scoped) && p.ns_location_id)
      ? " AND " + alias + ".location=" + Number(p.ns_location_id) : "";
  }

  // ---- READ actions (params are NetSuite internal ids from the app's customer record) ----
  // Sanitise an id list arriving from the app before it goes into SQL. Everything here is
  // server-to-server behind TBA, but this string is the only place app-supplied values reach
  // a query, so it is filtered to integers rather than trusted.
  function idList(vals) {
    var out = [];
    (vals || []).forEach(function (v) {
      var n = Number(v);
      if (n && isFinite(n)) out.push(String(Math.trunc(n)));
    });
    return out;
  }

  function invoices(p) {
    // The bill-to record may also be a trading entity (Mova invoices to Spacewalker HK
    // `11066`), in which case its ordinary product invoices must not appear in the 3PL
    // portal. When the app sends invoice_items_only + charge_item_ids, restrict to invoices
    // carrying at least one 3PL service item. Off by default, and skipped when no ids are
    // supplied — a customer whose invoices carry no charge item at all (Skriva's are $0
    // product invoices) would otherwise lose its whole Invoices view.
    var items = idList(p.charge_item_ids);
    var itemFilter = (truthy(p.invoice_items_only) && items.length)
      ? " AND EXISTS (SELECT 1 FROM transactionline il WHERE il.transaction=t.id " +
        "AND il.mainline='F' AND il.item IN (" + items.join(',') + "))" : "";
    // currency: the push never sets it (NetSuite sources it from the customer record), so
    // pulling it back is the only way a rate-card/invoice currency mismatch is ever visible.
    // amountremaining/duedate carry the payment state the portal shows next to the status.
    var heads = runSuiteQL(
      "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.status) status, t.foreigntotal total, " +
      "BUILTIN.DF(t.currency) currency, t.foreignamountunpaid unpaid, t.duedate, " +
      "t.lastmodifieddate " +
      "FROM transaction t WHERE t.type='CustInvc' AND t.entity=" + Number(p.ns_customer_id) +
      itemFilter);
    return heads.map(function (h) {
      // item id (not just its display name) is what lets the app match a line edited in
      // NetSuite back to the billing line that raised it — descriptions get edited too.
      var lines = runSuiteQL(
        "SELECT item, BUILTIN.DF(item) item_name, memo, quantity, rate, netamount " +
        "FROM transactionline WHERE transaction=" + Number(h.id) +
        " AND mainline='F' AND taxline='F' ORDER BY linesequencenumber");
      return {
        ns_invoice_id: String(h.id), tranid: h.tranid, trandate: h.trandate,
        status: h.status, total: h.total, currency: h.currency,
        amount_remaining: h.unpaid, due_date: h.duedate,
        ns_lastmodified: h.lastmodifieddate,
        lines: lines.map(function (l) {
          return { ns_item_id: l.item ? String(l.item) : null,
                   description: l.memo || l.item_name, qty: l.quantity,
                   rate: l.rate, amount: l.netamount };
        })
      };
    });
  }

  function purchaseOrders(p) {
    // Scope by vendor + the item's brand class, plus the 3PL location for location_scoped customers
    // (the PO line DOES carry the receiving location — verified 2026-07-22 — so a Mova-brand PO
    // receiving into a regular warehouse is correctly excluded from the 3PL portal).
    // tl.expectedreceiptdate is the PO line's own ETA. It IS selectable (validated against live
    // data 2026-07-26) — omitting it was why the portal's Expected Receipt column was always blank
    // for lines not yet on an inbound shipment, and why those units never reached the Incoming
    // chart. The shipment's expecteddeliverydate still wins at read time when one exists
    // (service.stock_on_order) — a booked container beats the line's planning date.
    var flat = runSuiteQL(
      "SELECT t.id, t.tranid, t.trandate, BUILTIN.DF(t.status) status, tl.item, " +
      "tl.quantity ordered, tl.quantityshiprecv received, tl.expectedreceiptdate expected " +
      "FROM transaction t " +
      "JOIN transactionline tl ON tl.transaction=t.id JOIN item i ON i.id=tl.item " +
      "WHERE t.type='PurchOrd' AND t.entity=" + Number(p.ns_supplier_id) +
      " AND i.class=" + Number(p.ns_class_id) + locClause(p, 'tl') +
      " AND tl.mainline='F' AND tl.taxline='F' AND tl.quantityshiprecv < tl.quantity");
    return group(flat, 'id',
      function (r) { return { ns_po_id: String(r.id), tranid: r.tranid, trandate: r.trandate, status: r.status }; },
      function (r) { return { ns_item_id: String(r.item), qty_ordered: r.ordered, qty_received: r.received,
                              expected_date: r.expected || null }; });
  }

  function itemReceipts(p) {
    // Scope by the ITEM's brand class (reliably set on the item; a manually-keyed receipt line
    // often has no class) so shared locations don't leak other customers' receipts, PLUS the 3PL
    // location for location_scoped customers — otherwise Mova's regular MOVA brand pulls in every
    // owned-inventory receipt into regular warehouses, not just 3PL putaways (fixed 2026-07-22).
    // The receipts themselves come first, on their own, with NO subquery. This read drives the
    // putaway charge, so it must not be able to fail for a decorative reason (see below).
    var flat = runSuiteQL(
      "SELECT t.id, t.tranid, t.trandate, tl.item, tl.quantity FROM transaction t " +
      "JOIN transactionline tl ON tl.transaction=t.id JOIN item i ON i.id=tl.item " +
      "WHERE t.type='ItemRcpt' AND i.class=" + Number(p.ns_class_id) + locClause(p, 'tl') +
      " AND tl.mainline='F' AND tl.taxline='F' AND t.trandate >= " + sinceExpr(p.since));

    // Two decorative columns on the portal's receipts view, both resolved from the receipt's
    // source PO in ONE lookup:
    //   po_tranid            — the source PO's document number. createdfrom is not selectable in
    //                          SuiteQL, so walk previoustransactionlinelink (receipt = nextdoc)
    //                          back to a PurchOrd. Null for a receipt from something else
    //                          (e.g. a transfer order).
    //   ns_inbound_shipment  — the container it arrived on. There is NO inboundshipment field on
    //                          the receipt (transaction.inboundshipment does not exist), and no
    //                          direct receipt->shipment link, so reach it through the PO:
    //                          receipt -> PO -> inboundshipmentitem.purchaseordertransaction ->
    //                          inboundshipment. VALIDATED against production 2026-07-27: all 9
    //                          Mova 3PL receipts resolve 1:1 to INBSHIP91-99.
    // The shipment joins are LEFT so a receipt whose PO isn't on a container still keeps its
    // po_tranid. MIN() picks one deterministically if a PO ever spans multiple shipments (note
    // it's a lexical MIN on the doc number, so INBSHIP100 would sort before INBSHIP97).
    //
    // Fetched SEPARATELY and in a try/catch, on purpose. po_tranid used to be a correlated
    // subquery in the SELECT list above, which made the *entire* receipts read all-or-nothing:
    // any problem reaching previoustransactionlinelink lost all 9 receipts AND the whole putaway
    // charge, to populate one cosmetic column. Now a failure blanks these two columns and nothing
    // else. Scoped by receipt id — the same id-list pattern inboundShipments uses — so it stays
    // cheap and can't pull in other subsidiaries' receipts.
    //
    // ⚠️ ns_inbound_shipment is NO LONGER COSMETIC (2026-08-01). billing.py now derives the
    // container-unload charge from it — receipt -> shipment, dated by the receipt's trandate —
    // because the shipment header's own dates are unusable (see inboundShipments below). So a
    // blank here is a $1,500-per-container under-bill, not a missing decoration. The try/catch
    // stays (losing the receipts would also lose putaway, which is worse), but the app now
    // WARNS on the billing preview for any receipt in the period with no shipment linked —
    // don't "simplify" that warning away.
    var extra = {}, warn = null, rcptIds = {};
    flat.forEach(function (r) { rcptIds[String(r.id)] = true; });
    var idList = Object.keys(rcptIds);
    if (idList.length) {
      try {
        runSuiteQL(
          "SELECT ptll.nextdoc receipt, MIN(po.tranid) po_tranid, MIN(s.shipmentnumber) shipment " +
          "FROM previoustransactionlinelink ptll " +
          "JOIN transaction po ON po.id=ptll.previousdoc " +
          "LEFT JOIN inboundshipmentitem isi ON isi.purchaseordertransaction=po.id " +
          "LEFT JOIN inboundshipment s ON s.id=isi.inboundshipment " +
          "WHERE po.type='PurchOrd' AND ptll.nextdoc IN (" + idList.join(',') + ") " +
          "GROUP BY ptll.nextdoc"
        ).forEach(function (r) {
          extra[String(r.receipt)] = { po: r.po_tranid || null, ship: r.shipment || null };
        });
      } catch (e) {
        // Leave both columns blank rather than losing the receipts. Surfaced on the response so
        // it doesn't fail silently — n8n logs the whole payload.
        warn = (e && e.message) || String(e);
      }
    }
    var out = group(flat, 'id',
      function (r) {
        var x = extra[String(r.id)] || {};
        return { ns_receipt_id: String(r.id), tranid: r.tranid, trandate: r.trandate,
                 po_tranid: x.po || null, ns_inbound_shipment: x.ship || null };
      },
      function (r) { return { ns_item_id: String(r.item), qty: r.quantity }; });
    if (warn && out.length) out[0].po_lookup_warning = warn;   // ignored by the app's ingest
    return out;
  }

  function itemFulfilments(p) {
    // Picking source = the fulfilment entity: a $0 SO ships to the customer, a VRMA ships the
    // stock back to the supplier. Both are type 'ItemShip'. Count units off the ASSET (inventory)
    // line and take ABS(): a customer SO emits a +qty COGS / -qty ASSET pair, while a VRMA emits a
    // single NEGATIVE ASSET line with no positive counterpart — so the old "tl.quantity > 0" filter
    // silently dropped every VRMA (stock left NetSuite but nothing showed here). The ASSET line is
    // the real inventory movement in both cases (negative = leaving), so ABS() yields picked units
    // and naturally de-dups the SO ± pair to one row per item.
    var flat = runSuiteQL(
      "SELECT t.id, t.tranid, t.trandate, t.entity, tl.item, ABS(tl.quantity) qty FROM transaction t " +
      "JOIN transactionline tl ON tl.transaction=t.id JOIN item i ON i.id=tl.item " +
      "WHERE t.type='ItemShip' AND t.entity IN (" +
      Number(p.ns_customer_id) + "," + Number(p.ns_supplier_id) + ") AND i.class=" +
      Number(p.ns_class_id) + locClause(p, 'tl') + " AND tl.mainline='F' AND tl.taxline='F' " +
      "AND tl.accountinglinetype='ASSET' AND tl.quantity IS NOT NULL AND tl.quantity <> 0 " +
      "AND t.trandate >= " + sinceExpr(p.since));
    var custId = String(p.ns_customer_id);

    // ns_source_id: the ORIGINATING document number (the $0 sales order, or the VRMA) that this
    // fulfilment was created from — the portal's "Reference" column. It was never returned at
    // all, so that column has always been blank for every customer.
    // createdfrom is not selectable in SuiteQL, so walk previoustransactionlinelink the same way
    // itemReceipts does (fulfilment = nextdoc, source = previousdoc). Deliberately NO filter on
    // the source transaction type: a fulfilment's previous doc IS its source order, whichever
    // type that is. VALIDATED against production 2026-07-27 — all 27 Skriva fulfilments resolved
    // to their SO ('SalesOrd' -> SO328496 etc.), and vendor-entity fulfilments resolve to
    // 'VendAuth' -> VRMA000327 etc. A guessed type filter (e.g. 'RtnAuth') would have silently
    // returned nothing for every VRMA — which is Mova's DEFAULT dispatch path.
    // GROUP BY + MIN() is required, not cosmetic: a fulfilment has one link row per line.
    // Separate, id-scoped and try/catch'd for the same reason as the receipts lookup — this is a
    // cosmetic column and must never be able to take the fulfilments (and the picking charge)
    // down with it.
    var refById = {};
    var ffIds = {};
    flat.forEach(function (r) { ffIds[String(r.id)] = true; });
    var ffList = Object.keys(ffIds);
    if (ffList.length) {
      try {
        runSuiteQL(
          "SELECT ptll.nextdoc ff, MIN(src.tranid) ref " +
          "FROM previoustransactionlinelink ptll " +
          "JOIN transaction src ON src.id=ptll.previousdoc " +
          "WHERE ptll.nextdoc IN (" + ffList.join(',') + ") " +
          "GROUP BY ptll.nextdoc"
        ).forEach(function (r) { refById[String(r.ff)] = r.ref || null; });
      } catch (e) {
        refById.__error = (e && e.message) || String(e);
      }
    }
    var refWarn = refById.__error || null;
    var ffOut = group(flat, 'id',
      function (r) { return { ns_fulfilment_id: String(r.id), tranid: r.tranid, trandate: r.trandate,
                              source_type: String(r.entity) === custId ? 'SO' : 'VRMA',
                              ns_source_id: refById[String(r.id)] || null }; },
      function (r) { return { ns_item_id: String(r.item), qty: r.qty }; });
    if (refWarn && ffOut.length) ffOut[0].source_lookup_warning = refWarn;
    return ffOut;
  }

  function stockOnHand(p) {
    // inventorybalance has multiple rows per item (status/bin); sum to one net qty/item. Scope by
    // the item's brand class, plus the 3PL location for location_scoped customers (Mova — so owned
    // MOVA stock in other warehouses is excluded). A non-location_scoped customer (Skriva) sums its
    // brand across all its locations, since the brand is 3PL-exclusive.
    var flat = runSuiteQL(
      "SELECT ib.item, SUM(ib.quantityonhand) qty FROM inventorybalance ib " +
      "JOIN item i ON i.id=ib.item WHERE i.class=" + Number(p.ns_class_id) +
      locClause(p, 'ib') + " GROUP BY ib.item");
    return flat.map(function (r) { return { ns_item_id: String(r.item), qty_on_hand: r.qty }; });
  }

  // item master for the customer's brand (class) — itemid = SKU shown in the portal; the portal
  // resolves fact rows' internal ids to these. units_per_pallet is a custom item field — pass its
  // field id in p.upp_field to include it (e.g. 'custitem_units_per_pallet'), else it stays null.
  function items(p) {
    // itemid = SKU; displayname = the readable name shown in the portal (salesdescription is not a
    // SuiteQL item column; description is usually null). units_per_pallet via optional custom field.
    var cols = "id, itemid, displayname, description";
    if (p.upp_field) cols += ", " + String(p.upp_field).replace(/[^a-z0-9_]/gi, '') + " upp";
    var flat = runSuiteQL("SELECT " + cols + " FROM item WHERE class=" + Number(p.ns_class_id));
    return flat.map(function (r) {
      return { ns_item_id: String(r.id), sku: r.itemid,
               description: r.displayname || r.description || null,
               units_per_pallet: (r.upp === undefined ? null : r.upp) };
    });
  }

  function inboundShipments(p) {
    // Inbound shipments (containers) for this customer's brand. Drives the container-unload
    // charge AND (via member lines) the inbound-shipment + expected-receipt columns on the
    // portal's Stock on order view.
    //
    // VALIDATED against production schema 2026-06-30 (see docs/netsuite_validation.md):
    //  - inboundshipment header fields: shipmentnumber, expecteddeliverydate, actualdeliverydate,
    //    and shipmentstatus (already a TEXT label like 'received' — do NOT wrap in BUILTIN.DF).
    //  - inboundshipmentitem has NO `item` column: the PO line (and thus the item) is reached via
    //    shipmentitemtransaction = transactionline.uniquekey; the PO header via purchaseordertransaction.
    //  - scope by the item's brand class (i.class), plus the 3PL location (via the PO line) for
    //    location_scoped customers — same per-customer isolation as the other reads.
    var cls = Number(p.ns_class_id);
    // Member lines: shipment id + PO doc number + item, for the Stock-on-order PO->shipment link.
    var members = runSuiteQL(
      "SELECT isi.inboundshipment shipment, po.tranid po_tranid, tl.item " +
      "FROM inboundshipmentitem isi " +
      "JOIN transactionline tl ON tl.uniquekey = isi.shipmentitemtransaction " +
      "JOIN item i ON i.id = tl.item " +
      "LEFT JOIN transaction po ON po.id = isi.purchaseordertransaction " +
      "WHERE i.class = " + cls + locClause(p, 'tl'));
    var membersByShip = {}, ids = {};
    members.forEach(function (m) {
      var k = String(m.shipment); ids[k] = true;
      (membersByShip[k] = membersByShip[k] || []).push(
        { ns_item_id: String(m.item), po_tranid: m.po_tranid || null });
    });
    var idList = Object.keys(ids);
    if (!idList.length) return [];
    // externaldocumentnumber = the CONTAINER number (e.g. CSNU8516117) — validated populated on
    // all 12 live Mova shipments 2026-07-27. Distinct from container_type (the "40ft loose
    // stacked" descriptor), which has no native field and stays null.
    var heads = runSuiteQL(
      "SELECT id, shipmentnumber, externaldocumentnumber, expecteddeliverydate, " +
      "actualdeliverydate, lastmodifieddate, shipmentstatus " +
      "FROM inboundshipment WHERE id IN (" + idList.join(',') + ")");
    // received_date is a RECEIVED/NOT-RECEIVED FLAG and a display date. It no longer drives
    // billing — do not restore that. VALIDATED against production 2026-07-31: actualdeliverydate
    // is NULL on all 36 Mova 3PL shipments, so the lastmodifieddate fallback below always applies,
    // and lastmodifieddate is the timestamp of whoever last touched the record, not a delivery.
    // All 36 clustered onto four bulk-edit timestamps (9 containers physically unloaded 20 Jul
    // read as 30 Jul), which billed 0 containers in the 20-26 Jul week and 22 in the next one.
    // billing.py now dates the container-unload charge off the ITEM RECEIPT's trandate instead.
    // Still useful here: the PRESENCE of received_date is a reliable "status is received /
    // partiallyReceived" test (in-transit shipments get none), which billing.py uses to flag
    // containers that landed but have no receipt pointing at them.
    var STATUS = { received: 'received', partiallyReceived: 'partially received',
                   inTransit: 'in transit' };
    return heads.map(function (h) {
      var isReceived = h.shipmentstatus === 'received' || h.shipmentstatus === 'partiallyReceived';
      return { ns_shipment_id: String(h.id), shipment_number: h.shipmentnumber,
               container_type: null,  // no native container-type field on inboundshipment
               container_no: h.externaldocumentnumber || null,
               expected_date: h.expecteddeliverydate,
               received_date: isReceived ? (h.actualdeliverydate || h.lastmodifieddate) : null,
               status: STATUS[h.shipmentstatus] || h.shipmentstatus,
               lines: membersByShip[String(h.id)] || [] };
    });
  }

  function sinceExpr(since) {
    // since = 'YYYY-MM-DD' (default 2020-01-01); SuiteQL date literal
    var d = (since && /^\d{4}-\d{2}-\d{2}$/.test(since)) ? since : '2020-01-01';
    return "TO_DATE('" + d + "','YYYY-MM-DD')";
  }

   // ---- WRITE action: create a DRAFT invoice from billing lines ----
  function createInvoice(p) {
    var rec = record.create({ type: record.Type.INVOICE, isDynamic: true });
    rec.setValue({ fieldId: 'entity', value: Number(p.ns_customer_id) });
    if (p.ns_subsidiary_id) rec.setValue({ fieldId: 'subsidiary', value: Number(p.ns_subsidiary_id) });
    // Location is mandatory on transactions in this account — set it on the header (cascades to
    // lines) and on each line for accounts that require it line-level.
    if (p.ns_location_id) rec.setValue({ fieldId: 'location', value: Number(p.ns_location_id) });
    if (p.memo) rec.setValue({ fieldId: 'memo', value: p.memo });
    if (!(p.lines || []).length) throw new Error('create_invoice: no lines supplied');
    (p.lines || []).forEach(function (l, i) {
      // item_id is resolved app-side from the charge-item catalogue (it used to come from a
      // CHARGE_ITEMS map hardcoded in the n8n node, whose ids were sandbox-only). Refuse
      // rather than let NetSuite create a line against item `NaN`.
      if (!Number(l.item_id)) {
        throw new Error('create_invoice: line ' + (i + 1) + ' (' +
                        (l.description || l.charge_type || '?') + ') has no NetSuite item id');
      }
      rec.selectNewLine({ sublistId: 'item' });
      rec.setCurrentSublistValue({ sublistId: 'item', fieldId: 'item', value: Number(l.item_id) });
      rec.setCurrentSublistValue({ sublistId: 'item', fieldId: 'quantity', value: Number(l.qty) });
      rec.setCurrentSublistValue({ sublistId: 'item', fieldId: 'rate', value: Number(l.rate) });
      if (l.description) rec.setCurrentSublistValue({ sublistId: 'item', fieldId: 'description', value: l.description });
      if (p.ns_location_id) rec.setCurrentSublistValue({ sublistId: 'item', fieldId: 'location', value: Number(p.ns_location_id) });
      rec.commitLine({ sublistId: 'item' });
    });
    var id = rec.save({ enableSourcing: true, ignoreMandatoryFields: false });
    return { ns_invoice_id: String(id) };
  }

  var ACTIONS = {
    items: items,
    invoices: invoices, purchase_orders: purchaseOrders, item_receipts: itemReceipts,
    item_fulfilments: itemFulfilments, stock_on_hand: stockOnHand,
    inbound_shipments: inboundShipments,
    create_invoice: createInvoice
  };

  function post(body) {
    try {
      var action = body && body.action;
      if (!ACTIONS[action]) return { error: 'unknown action: ' + action };
      return { ok: true, data: ACTIONS[action](body) };
    } catch (e) {
      return { error: (e && e.name) || 'error', message: (e && e.message) || String(e) };
    }
  }

  return { post: post };
});
