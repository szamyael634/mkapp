(function () {
  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function exportPdf(options) {
    const title = options.title || 'Mama's Kitchen Report';
    const subtitle = options.subtitle || '';
    const columns = options.columns || [];
    const rows = options.rows || [];
    const summary = options.summary || [];
    const generatedAt = new Date().toLocaleString();

    const summaryHtml = summary.length
      ? `<div class="summary">${summary.map((item) => `
          <div>
            <span>${escapeHtml(item.label)}</span>
            <strong>${escapeHtml(item.value)}</strong>
          </div>
        `).join('')}</div>`
      : '';

    const bodyRows = rows.length
      ? rows.map((row) => `
          <tr>
            ${columns.map((column) => `
              <td class="${column.align === 'right' ? 'right' : ''}">
                ${escapeHtml(row[column.key])}
              </td>
            `).join('')}
          </tr>
        `).join('')
      : `<tr><td colspan="${columns.length}" class="empty">No report data available.</td></tr>`;

    const reportWindow = window.open('', '_blank', 'width=1100,height=800');
    if (!reportWindow) {
      alert('Please allow pop-ups to export the PDF.');
      return;
    }

    reportWindow.document.write(`
      <!doctype html>
      <html>
        <head>
          <meta charset="utf-8">
          <title>${escapeHtml(title)}</title>
          <style>
            * { box-sizing: border-box; }
            body {
              font-family: Arial, sans-serif;
              color: #1f2937;
              margin: 28px;
            }
            header {
              border-bottom: 2px solid #f59e0b;
              margin-bottom: 18px;
              padding-bottom: 12px;
            }
            h1 {
              font-size: 24px;
              margin: 0 0 6px;
            }
            .subtitle {
              color: #4b5563;
              font-size: 13px;
              margin: 0;
            }
            .generated {
              color: #6b7280;
              font-size: 12px;
              margin-top: 6px;
            }
            .summary {
              display: grid;
              grid-template-columns: repeat(3, 1fr);
              gap: 10px;
              margin: 18px 0;
            }
            .summary div {
              border: 1px solid #e5e7eb;
              border-radius: 8px;
              padding: 10px;
              background: #fffbeb;
            }
            .summary span {
              display: block;
              color: #6b7280;
              font-size: 11px;
              text-transform: uppercase;
              margin-bottom: 4px;
            }
            .summary strong {
              display: block;
              font-size: 16px;
            }
            table {
              width: 100%;
              border-collapse: collapse;
              font-size: 12px;
            }
            th {
              background: #fef3c7;
              color: #374151;
              text-align: left;
              text-transform: uppercase;
              font-size: 11px;
            }
            th, td {
              border: 1px solid #e5e7eb;
              padding: 8px;
              vertical-align: top;
            }
            td.right, th.right {
              text-align: right;
            }
            tr:nth-child(even) td {
              background: #fafafa;
            }
            .empty {
              text-align: center;
              color: #6b7280;
              padding: 18px;
            }
            @page {
              size: A4 landscape;
              margin: 12mm;
            }
            @media print {
              body { margin: 0; }
            }
          </style>
        </head>
        <body>
          <header>
            <h1>${escapeHtml(title)}</h1>
            ${subtitle ? `<p class="subtitle">${escapeHtml(subtitle)}</p>` : ''}
            <div class="generated">Generated: ${escapeHtml(generatedAt)}</div>
          </header>
          ${summaryHtml}
          <table>
            <thead>
              <tr>
                ${columns.map((column) => `
                  <th class="${column.align === 'right' ? 'right' : ''}">
                    ${escapeHtml(column.label)}
                  </th>
                `).join('')}
              </tr>
            </thead>
            <tbody>${bodyRows}</tbody>
          </table>
          <script>
            window.onload = function () {
              window.focus();
              window.print();
            };
          <\/script>
        </body>
      </html>
    `);
    reportWindow.document.close();
  }

  window.PetopiaReports = {
    exportPdf,
  };
})();
