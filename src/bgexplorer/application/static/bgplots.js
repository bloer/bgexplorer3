/* Interactive plots for Background Explorer, drawn with plotly.js from the
 * JSON produced by application/plotting.py
 */
const bgplots = (function(){
    "use strict";

    const COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'];
    const MEASURED_COLOR = '#d62728';
    const LIMIT_COLOR = '#1f4fd6';
    // spectrum totals, and the sum of the parts not shown
    const TOTAL_COLOR = '#212529';
    const OTHER_COLOR = '#adb5bd';
    // responsive plots fill their div's height, so layouts must set one or
    // the div collapses after the first redraw and the plot overflows it
    const CONFIG = {responsive: true, displaylogo: false};
    const SPECTRUM_HEIGHT = 450;

    function el(tag, attrs, children){
        const node = document.createElement(tag);
        for(const [key, value] of Object.entries(attrs || {})){
            if(key === 'text')
                node.textContent = value;
            else
                node.setAttribute(key, value);
        }
        (children || []).forEach(child => node.appendChild(child));
        return node;
    }

    function getJSON(url){
        return fetch(url).then(response => response.json().then(data => {
            if(!response.ok)
                throw new Error((data.error && data.error.message) || response.statusText);
            return data;
        }));
    }

    function showError(div, error){
        if(window.Plotly)
            Plotly.purge(div);
        div.replaceChildren(el('div', {'class': 'alert alert-danger',
                                       text: String(error.message || error)}));
    }

    /* Run `load` the first time `div` is visible, e.g. in a hidden tab */
    function whenVisible(div, load){
        if(!('IntersectionObserver' in window)){
            load();
            return;
        }
        const observer = new IntersectionObserver(entries => {
            if(entries.some(entry => entry.isIntersecting)){
                observer.disconnect();
                load();
            }
        });
        observer.observe(div);
    }

    function toggle(id, label, checked){
        const input = el('input', {'class': 'form-check-input', type: 'checkbox', id: id});
        input.checked = checked;
        return [input, el('div', {'class': 'form-check form-check-inline'},
                          [input, el('label', {'class': 'form-check-label', 'for': id, text: label})])];
    }

    function unitLabel(units){
        return units ? ` [${units}]` : '';
    }

    /* Step coordinates over the bins where `keep(i)`: each bin is drawn
     * from its low to high edge at `yval(i)`, with a gap for other bins
     */
    function steps(h, keep, yval){
        const x = [], y = [];
        h.value.forEach((v, i) => {
            if(!keep(i)){
                if(x.length && x[x.length-1] !== null){
                    x.push(null);
                    y.push(null);
                }
                return;
            }
            x.push(h.bins[i], h.bins[i+1]);
            y.push(yval(i), yval(i));
        });
        return [x, y];
    }

    /* log10 y range covering the values and upper limits of histograms
     * `hists`, padded, or null if none are positive. Lower errors may run
     * off the bottom
     */
    function spectrumRange(hists){
        const points = [];
        hists.forEach(h => h.value.forEach((v, i) => points.push(v, h.upper_limit[i])));
        const positive = points.filter(v => v > 0);
        if(!positive.length)
            return null;
        return [Math.log10(Math.min(...positive)) - 0.2,
                Math.log10(Math.max(...positive)) + 0.2];
    }

    /* Traces for one histogram: a step line for measured bins and, if
     * `band`, a shaded band for every bin from its lower error (zero for
     * upper limits) to its one-sided 1 sigma upper limit, so a measurement
     * plus a limit is never drawn below the limit alone. Lower edges at or
     * below zero are drawn at `floor`, e.g. below a log axis. Without a
     * band, upper limit bins are a dashed step at the limit
     */
    function spectrumTraces(name, h, color, floor = 0, band = true){
        const limit = i => h.is_limit && h.is_limit[i];
        const measured = i => !limit(i);
        const [x, y] = steps(h, measured, i => h.value[i]);
        const traces = [
            {x: x, y: y, name: name, legendgroup: name, mode: 'lines', connectgaps: false,
             line: {color: color, width: 1.5}},
        ];
        if(band){
            const all = () => true;
            const [bx, low] = steps(h, all, i => {
                const lo = limit(i) ? 0 : h.value[i] - h.err_minus[i];
                return lo > 0 ? lo : floor;
            });
            const [, high] = steps(h, all, i => h.upper_limit[i]);
            // one closed polygon, high then low
            traces.unshift(
                {x: bx.concat(bx.slice().reverse()), y: high.concat(low.slice().reverse()),
                 mode: 'lines', fill: 'toself', fillcolor: color + '40',
                 line: {width: 0}, legendgroup: name, showlegend: false, hoverinfo: 'skip'});
        } else if(h.value.some((v, i) => limit(i))){
            const [ulx, uly] = steps(h, limit, i => h.upper_limit[i]);
            traces.push(
                {x: ulx, y: uly, mode: 'lines', connectgaps: false, legendgroup: name,
                 showlegend: false, hoverinfo: 'skip',
                 line: {color: color, width: 1, dash: 'dash'}});
        }
        return traces;
    }

    /* Plot the spectra served as {name: histogram_json} by `url` into
     * `div`, with a selector for which spectra to show and log-scale
     * toggles. Data are fetched when the div first becomes visible.
     */
    function spectrum(div, url){
        if(typeof div === 'string')
            div = document.getElementById(div);
        whenVisible(div, () => {
            div.replaceChildren(el('div', {'class': 'spinner-border', role: 'status'}));
            getJSON(url).then(data => drawSpectra(div, data))
                        .catch(error => showError(div, error));
        });
    }

    /* Plot one spectrum served by `url` (spectrum.json) into `div`: its
     * total, with its uncertainty, and optionally its largest parts by a
     * groupby, with the rest as "other". Each part keeps its color while
     * filtering. If `dash` is a dashboard, the spectrum follows its
     * filters. Data are fetched when the div first becomes visible.
     */
    function spectrumBreakdown(div, url, dash){
        if(typeof div === 'string')
            div = document.getElementById(div);
        const id = div.id || 'spectrum';
        const select = el('select', {'class': 'form-select form-select-sm w-auto',
                                     id: id + '_spectrum', 'aria-label': 'spectrum to show'});
        const groupby = el('select', {'class': 'form-select form-select-sm w-auto',
                                      id: id + '_groupby', 'aria-label': 'break down by'},
            [['', 'total only'], ['component', 'by component'], ['isotope', 'by isotope'],
             ['material', 'by material'], ['category', 'by source category']]
                .map(([value, text]) => el('option', {value: value, text: text})));
        const [logx, logxdiv] = toggle(id + '_logx', 'log x', false);
        const [logy, logydiv] = toggle(id + '_logy', 'log y', true);
        const plot = el('div', {'class': 'bgplot'});
        const controls = el('div', {'class': 'd-flex gap-2 align-items-center mb-2'}, [
            el('label', {'for': select.id, text: 'Spectrum'}), select,
            el('label', {'class': 'ms-2', 'for': groupby.id, text: 'Show'}), groupby,
            el('div', {'class': 'ms-2'}, [logxdiv]), logydiv]);
        div.replaceChildren(controls, plot);
        div.plot = plot;

        const cache = {};
        let filters = JSON.stringify({root: []}), filtered = false;
        let started = false, request = 0, data = null;

        function load(){
            const params = {spectrum: select.value, groupby: groupby.value, filters: filters};
            const key = JSON.stringify(params);
            if(!cache[key]){
                const sep = url.includes('?') ? '&' : '?';
                cache[key] = getJSON(url + sep + new URLSearchParams(params));
                cache[key].catch(() => delete cache[key]);
            }
            return cache[key];
        }

        function message(text){
            Plotly.purge(plot);
            plot.replaceChildren(el('p', {'class': 'text-secondary', text: text}));
        }

        function draw(){
            if(!data)
                return;
            if(!data.total)
                return message(filtered ? 'Nothing passing the filters has this spectrum'
                                        : 'No spectra');
            if(!plot._fullLayout)
                plot.replaceChildren();
            const hists = [data.total].concat(data.curves.map(c => c.value),
                                              data.other ? [data.other] : []);
            const range = logy.checked ? spectrumRange(hists) : null;
            const floor = range ? 10 ** (range[0] - 1) : 0;
            const traces = spectrumTraces(filtered ? 'Filtered total' : 'Total', data.total,
                                          TOTAL_COLOR, floor);
            data.curves.forEach(c => traces.push(...spectrumTraces(
                String(c.label), c.value, COLORS[c.rank % COLORS.length], floor, false)));
            if(data.other)
                traces.push(...spectrumTraces('other', data.other, OTHER_COLOR, floor, false));
            const layout = {
                height: SPECTRUM_HEIGHT,
                margin: {t: 20, r: 10},
                xaxis: {title: {text: 'Energy' + unitLabel(data.total.binsunit)},
                        type: logx.checked ? 'log' : 'linear'},
                yaxis: {title: {text: unitLabel(data.total.units).trim()},
                        type: logy.checked ? 'log' : 'linear', exponentformat: 'power',
                        range: range || undefined},
                showlegend: data.curves.length > 0,
            };
            Plotly.react(plot, traces, layout, CONFIG);
        }

        function update(){
            const mine = ++request;
            div.classList.add('loading');
            load().then(result => {
                if(mine !== request)
                    return;
                div.classList.remove('loading');
                if(!result.spectra.length){
                    controls.remove();
                    data = null;
                    return message('No spectra');
                }
                if(!select.options.length){
                    select.replaceChildren(...result.spectra.map(
                        s => el('option', {value: s.key, text: s.name})));
                    select.value = result.spectrum;
                }
                data = result;
                draw();
            }).catch(error => {
                if(mine !== request)
                    return;
                div.classList.remove('loading');
                showError(plot, error);
            });
        }

        [select, groupby].forEach(input => input.addEventListener('change', update));
        [logx, logy].forEach(input => input.addEventListener('change', draw));
        if(dash)
            dash.onFilter((f, json) => {
                filters = json;
                filtered = !!(f.root && f.root.length) || Object.keys(f).some(
                    key => key !== 'root' && f[key].length);
                if(started)
                    update();
            });
        whenVisible(div, () => {
            started = true;
            update();
        });
    }

    function drawSpectra(div, data){
        const names = Object.keys(data);
        if(!names.length){
            div.replaceChildren(el('p', {'class': 'text-secondary', text: 'No spectra'}));
            return;
        }
        const id = div.id || 'spectrum';
        const select = el('select', {'class': 'form-select form-select-sm w-auto',
                                     multiple: '', size: String(Math.min(names.length, 4)),
                                     'aria-label': 'spectra to show'},
                          names.map(name => el('option', {value: name, text: name})));
        select.options[0].selected = true;
        const [logx, logxdiv] = toggle(id + '_logx', 'log x', false);
        const [logy, logydiv] = toggle(id + '_logy', 'log y', true);
        const plot = el('div', {'class': 'bgplot'});
        const controls = el('div', {'class': 'd-flex gap-3 align-items-center mb-2'},
                            [select, logxdiv, logydiv]);
        div.replaceChildren(controls, plot);
        div.plot = plot;

        function draw(){
            const selected = Array.from(select.selectedOptions).map(o => o.value);
            const range = logy.checked ? spectrumRange(selected.map(name => data[name])) : null;
            const floor = range ? 10 ** (range[0] - 1) : 0;
            const traces = [];
            selected.forEach((name, i) => traces.push(
                ...spectrumTraces(name, data[name], COLORS[i % COLORS.length], floor)));
            const first = data[selected[0]] || {};
            const layout = {
                height: SPECTRUM_HEIGHT,
                margin: {t: 20, r: 10},
                xaxis: {title: {text: 'Energy' + unitLabel(first.binsunit)},
                        type: logx.checked ? 'log' : 'linear'},
                yaxis: {title: {text: unitLabel(first.units).trim()},
                        type: logy.checked ? 'log' : 'linear', exponentformat: 'power',
                        range: range || undefined},
                showlegend: selected.length > 1,
            };
            Plotly.react(plot, traces, layout, CONFIG);
        }
        [select, logx, logy].forEach(input => input.addEventListener('change', draw));
        draw();
    }

    /* Traces for budget `rows`, each with a unique `id` for its y category
     * and a `value`, the sum of its measurements and upper limits. Values
     * are red points with error bars, and upper limits (if everything in
     * the row is one) blue lines from the edge of the plot ending in an
     * arrow at the 90% limit. Rows where `dim` is true are faded. Always the
     * same four traces, so plotly can animate between filters: faded and
     * unfaded limits, then faded and unfaded values
     */
    function budgetTraces(rows, dim, units){
        dim = dim || (() => false);
        const u = units ? ' ' + units : '';
        const values = rows.filter(r => r.value && !r.value.is_limit && r.value.value > 0);
        const limits = rows.filter(r => r.value && r.value.is_limit && r.value.upper_limit > 0);
        const limitTraces = [true, false].map(faded => {
            const rows = limits.filter(r => dim(r) === faded);
            return {type: 'scatter', mode: 'markers', name: '90% upper limit',
                    opacity: faded ? 0.25 : 1, showlegend: !faded,
                    ids: rows.map(r => r.id),
                    x: rows.map(r => r.value.upper_limit),
                    y: rows.map(r => r.id),
                    // far past the edge of any log axis, which clips it
                    error_x: {type: 'data', symmetric: false, width: 0, thickness: 2,
                              array: rows.map(() => 0),
                              arrayminus: rows.map(r => r.value.upper_limit * (1 - 1e-12)),
                              color: LIMIT_COLOR},
                    hovertemplate: `< %{x:.3g}${u}<extra></extra>`,
                    marker: {symbol: 'triangle-left', size: 10, color: LIMIT_COLOR}};
        });
        const valueTraces = [true, false].map(faded => {
            const rows = values.filter(r => dim(r) === faded);
            return {type: 'scatter', mode: 'markers', name: 'Value',
                    opacity: faded ? 0.25 : 1, showlegend: !faded,
                    ids: rows.map(r => r.id),
                    x: rows.map(r => r.value.value),
                    y: rows.map(r => r.id),
                    customdata: rows.map(r => [r.value.err_plus, r.value.err_minus]),
                    // TODO: the upper bar is value + err_plus, the usual 1 sigma
                    // error, but limits are drawn at their 90% upper limit. So
                    // a value plus a large limit (e.g. brass in the qis example)
                    // can reach less far than the limit alone. Consider drawing
                    // both at the same quantile, as the spectra do (see
                    // histogram_json's upper_limit)
                    error_x: {type: 'data', symmetric: false,
                              array: rows.map(r => r.value.err_plus),
                              // keep the lower bar on a log axis
                              arrayminus: rows.map(r => Math.min(r.value.err_minus,
                                                                 0.999 * r.value.value)),
                              color: MEASURED_COLOR},
                    hovertemplate: '%{x:.3g} (+%{customdata[0]:.2g} −%{customdata[1]:.2g})'
                                 + `${u}<extra></extra>`,
                    marker: {symbol: 'circle', size: 8, color: MEASURED_COLOR}};
        });
        return limitTraces.concat(valueTraces);
    }

    /* log10 x range covering everything drawn for `rows`, padded */
    function budgetRange(rows){
        const points = [];
        rows.forEach(r => {
            const v = r.value;
            if(v && v.is_limit && v.upper_limit > 0){
                points.push(v.upper_limit);
            } else if(v && v.value > 0){
                points.push(v.value, v.value + v.err_plus);
                if(v.value - v.err_minus > 0)
                    points.push(v.value - v.err_minus);
            }
        });
        if(!points.length)
            return null;
        const pad = 0.2;
        return [Math.log10(Math.min(...points)) - pad,
                Math.log10(Math.max(...points)) + pad];
    }

    function unionRange(a, b){
        if(!a || !b)
            return a || b;
        return [Math.min(a[0], b[0]), Math.max(a[1], b[1])];
    }

    function escapeText(text){
        return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                           .replace(/>/g, '&gt;');
    }

    // clicking this before an assembly's label shows what's inside it
    const DRILL = '▶';

    /* Mark the drill down markers in `plot`'s labels, with a tooltip */
    function titleDrill(plot){
        plot.querySelectorAll('.ytick tspan:not([data-drill])').forEach(tspan => {
            if(tspan.textContent !== DRILL)
                return;
            tspan.dataset.drill = '1';
            const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
            title.textContent = 'drill down';
            tspan.appendChild(title);
        });
    }

    /* The y axis label of a dashboard row, in plotly's pseudo-HTML */
    function tickText(row, highlighted, drillable){
        let text = escapeText(row.name);
        // right-justified, so children are marked at the end
        if(row.depth)
            text += ' ←';
        const empty = !row.value;
        const color = empty || row.selected === false ? '#adb5bd'
                    : row.depth ? '#6c757d' : null;
        if(color)
            text = `<span style="color:${color}">${text}</span>`;
        if(highlighted)
            text = `<b>${text}</b>`;
        if(drillable && row.children)
            text = `<span style="color:#0d6efd">${DRILL}</span> ` + text;
        return text;
    }

    /* Draw the budget `data.rows` into `plot`, hiding rows below
     * `threshold` of the total unfiltered size of the top level (depth 0)
     * rows. Rows in `highlight` are shaded, and rows with `selected` false
     * are faded. Changes to the same rows are animated. Returns the rows
     * drawn, whose y axis labels are in the same order
     */
    function drawBudget(plot, data, threshold, title, range, highlight, drillable){
        const total = data.rows.filter(r => !r.depth)
                               .reduce((sum, r) => sum + r.size, 0);
        const rows = data.rows.filter(r => r.size > 0 && r.size >= threshold * total);
        const ids = rows.map(r => r.id);
        const same = plot._fullLayout && sameKey(ids, plot._rowids);
        if(!same || !rows.length){
            // clear plotly's state, and its event handlers, along with any message
            Plotly.purge(plot);
            plot.replaceChildren();
            plot._bound = false;
        }
        plot._rowids = ids;
        if(!rows.length){
            plot.appendChild(el('p', {'class': 'text-secondary', text: 'No contributions'}));
            return rows;
        }
        const shapes = [];
        const band = (i, color) => shapes.push({
            type: 'rect', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: i - 0.5, y1: i + 0.5,
            layer: 'below', fillcolor: color, line: {width: 0}});
        // children of the row above
        rows.forEach((r, i) => { if(r.depth) band(i, '#f3f5f7'); });
        rows.forEach((r, i) => { if(highlight && highlight(r)) band(i, '#fff3cd'); });
        const layout = {
            title: {text: title, font: {size: 14}},
            margin: {t: 30, r: 10, l: 10},
            height: 120 + 22 * rows.length,
            xaxis: {type: 'log', title: {text: data.scalar + unitLabel(data.units)},
                    exponentformat: 'power', gridcolor: '#ddd', range: range},
            // the labels are clicked on to filter, so keep them uncovered
            // a fixed range, top row first: autorange would follow the rows
            // with values, and move them while filtering
            yaxis: {type: 'category', automargin: true, range: [ids.length - 0.5, -0.5],
                    fixedrange: true, categoryorder: 'array', categoryarray: ids,
                    tickmode: 'array', tickvals: ids,
                    ticktext: rows.map(r => tickText(r, highlight && highlight(r), drillable))},
            hovermode: 'closest',
            shapes: shapes,
            legend: {orientation: 'h', y: -0.15},
            showlegend: false,
        };
        const traces = budgetTraces(rows, r => r.selected === false, data.units);
        const drawing = plot._drawing = (plot._drawing || 0) + 1;
        if(same){
            // relabel, then move the values from where they were. The
            // animation doesn't draw the error bars of points moving
            // between traces, or always remove the old points, so redraw
            // the final state (already in plot.data, so react would skip
            // it) unless a newer draw has started
            const settle = () => {
                if(drawing === plot._drawing)
                    return Plotly.redraw(plot).then(() => titleDrill(plot));
            };
            Plotly.react(plot, plot.data, layout, CONFIG).then(() => {
                titleDrill(plot);
                return Plotly.animate(
                    plot, {data: traces, traces: traces.map((t, i) => i)},
                    {transition: {duration: 400, easing: 'cubic-in-out'},
                     frame: {duration: 400, redraw: false}, mode: 'immediate'});
            }).then(settle, settle);
        } else {
            Plotly.react(plot, traces, layout, CONFIG).then(() => titleDrill(plot));
        }
        return rows;
    }

    const GROUP_TITLES = {component: 'component', isotope: 'isotope',
                          material: 'material', category: 'source category'};

    /* A total from the server, formatted there as text and LaTeX, which
     * is typeset if MathJax is loaded
     */
    function formatValue(v, units){
        const u = units ? ' ' + units : '';
        if(!v)
            return '0' + u;
        if(window.MathJax && MathJax.typesetPromise && v.latex)
            return `\\(${v.latex}\\)${u}`;
        return v.text + u;
    }

    /* Rows with a unique `id` for their plotly category, and their label as
     * `name`
     */
    function displayRows(rows){
        const seen = new Set();
        return rows.map(r => {
            let id = r.label;
            while(seen.has(id))
                id += '\u200b';
            seen.add(id);
            return Object.assign({}, r, {name: r.label, id: id});
        });
    }

    function sameKey(a, b){
        return JSON.stringify(a) === JSON.stringify(b);
    }

    /* Filterable budget breakdowns of one component, one panel per groupby,
     * with a panel of the filters and the total passing them. `url` is the
     * dashboard.json endpoint, `scalars` the result names to choose from.
     *
     * Clicking a row's label keeps only its SourceTerms, ctrl-clicking
     * removes them, and shift-clicking collects several rows until shift is
     * released. Each panel is filtered by the other panels' selections,
     * and shades or fades its own. Clicking the ▶ before an assembly's
     * label drills into it. The rows stay put while filtering, and the
     * values move to their new places. All
     * sums, with their correlated uncertainties, are done by the server.
     * The state is kept in the URL's hash. All panels share one x range,
     * and zoom and pan together.
     *
     * Other views follow the filters through the returned object's
     * `onFilter(callback)`, which calls `callback(filters, json)` now and
     * after each change. `options.table: {id, url}` is a contributions
     * table to keep filtered this way (see `filteredTable`).
     */
    function dashboard(div, url, groupbys, scalars, options){
        if(typeof div === 'string')
            div = document.getElementById(div);
        const id = div.id || 'dashboard';
        const select = el('select', {'class': 'form-select form-select-sm w-auto',
                                     id: id + '_scalar', 'aria-label': 'result to plot'},
                          scalars.map(name => el('option', {value: name, text: name})));
        const threshold = el('input', {'class': 'form-control form-control-sm', type: 'number',
                                       min: '0', max: '100', step: 'any', value: '0',
                                       id: id + '_threshold', style: 'width: 6em'});
        const controls = el('div', {'class': 'd-flex gap-2 align-items-center mb-2'}, [
            el('label', {'for': select.id, text: 'Result'}), select,
            el('label', {'class': 'ms-3', 'for': threshold.id, text: 'Hide below'}), threshold,
            el('span', {text: '% of total'})]);
        const info = el('div', {'class': 'dashboard-info'});
        const breadcrumb = el('ol', {'class': 'breadcrumb mb-1 small'});
        const drill = el('div', {'class': 'dashboard-drill small'});
        const panels = groupbys.map(groupby => el('div', {'class': 'bgplot',
                                                          'data-groupby': groupby}));
        const columns = panels.map((panel, i) => el('div', {'class': 'col-xl-6'},
            groupbys[i] === 'component' ? [breadcrumb, drill, panel] : [panel]));
        div.replaceChildren(controls, el('div', {'class': 'row'}, [
            el('div', {'class': 'col-lg-3'}, [info]),
            el('div', {'class': 'col-lg-9'}, [el('div', {'class': 'row'}, columns)])]));
        div.panels = panels;

        // filters: {root: [placement ids], <groupby>: [{key, exclude, label}]}
        let filters = {root: []};
        const hash = new URLSearchParams(location.hash.slice(1));
        if(scalars.includes(hash.get('scalar')))
            select.value = hash.get('scalar');
        try {
            filters = Object.assign(filters, JSON.parse(hash.get('filters')) || {});
        } catch(e) {
            // ignore a garbled hash
        }
        const cache = {};
        const listeners = [];
        let current = null, request = 0, pending = false;
        // the shared range: the extent of everything shown, or a zoom
        let fullRange = null, range = null, syncing = false;

        function load(){
            const params = {scalar: select.value, filters: JSON.stringify(filters)};
            const key = JSON.stringify(params);
            if(!cache[key]){
                const sep = url.includes('?') ? '&' : '?';
                cache[key] = getJSON(url + sep + new URLSearchParams(params));
                cache[key].catch(() => delete cache[key]);
            }
            return cache[key];
        }

        // apply one panel's zoom to the others
        function sync(source, event){
            if(syncing)
                return;
            let next = null;
            if('xaxis.range[0]' in event && 'xaxis.range[1]' in event)
                next = [event['xaxis.range[0]'], event['xaxis.range[1]']];
            else if(Array.isArray(event['xaxis.range']))
                next = event['xaxis.range'].slice();
            else if(event['xaxis.autorange'])
                next = fullRange;
            if(!next)
                return;
            range = next;
            syncing = true;
            Promise.all(panels.filter(p => p !== source && p._fullLayout)
                              .map(p => Plotly.relayout(p, {'xaxis.range': range.slice()})))
                   .finally(() => { syncing = false; });
        }

        function entries(groupby){
            return filters[groupby] || [];
        }

        /* Change the filters for a click on `row` of `groupby` */
        function pick(groupby, row, event){
            if(row.key === null)
                return;
            const entry = {key: row.key, exclude: !!(event.ctrlKey || event.metaKey),
                           label: row.name};
            const others = entries(groupby).filter(e => !sameKey(e.key, entry.key));
            if(event.shiftKey){
                filters[groupby] = others.concat([entry]);
                pending = true;
                showInfo();
                return;
            }
            const same = entries(groupby).length === 1 && others.length === 0
                         && entries(groupby)[0].exclude === entry.exclude;
            // clicking the only selection again clears it
            filters[groupby] = same ? [] : [entry];
            update();
        }

        function remove(groupby, entry){
            filters[groupby] = entries(groupby).filter(e => e !== entry);
            update();
        }

        function setRoot(key){
            filters.root = key;
            // below the new root, only keep component filters inside it
            filters.component = entries('component').filter(
                e => e.key.length > key.length && sameKey(e.key.slice(0, key.length), key));
            update();
        }

        function reset(){
            filters = {root: []};
            update();
        }

        function link(text, onclick, attrs){
            const a = el('a', Object.assign({href: '#', text: text}, attrs || {}));
            a.addEventListener('click', event => {
                event.preventDefault();
                onclick();
            });
            return a;
        }

        function showInfo(){
            const children = [];
            const anyFilters = filters.root.length
                               || groupbys.some(groupby => entries(groupby).length);
            const total = (title, t, cls) => children.push(
                el('p', {'class': 'mb-2 ' + cls}, [
                    el('strong', {text: title + ': '}),
                    el('span', {text: formatValue(t, current.units)})]));
            if(current){
                children.push(el('p', {'class': 'mb-2'}, [
                    el('strong', {text: `${current.count}`}),
                    el('span', {text: ` of ${current.ntotal} source terms pass all filters`})]));
                if(anyFilters)
                    total('Filtered total', current.total, 'dashboard-total');
                total(anyFilters ? 'Unfiltered total' : 'Total', current.unfiltered,
                      'dashboard-unfiltered' + (anyFilters ? ' text-secondary' : ''));
            }
            const active = el('ul', {'class': 'list-unstyled mb-1 dashboard-filters'});
            groupbys.forEach(groupby => entries(groupby).forEach(entry => {
                const remover = link('✕', () => remove(groupby, entry),
                                     {'class': 'text-danger ms-1', title: 'remove this filter'});
                active.appendChild(el('li', {}, [
                    el('span', {'class': 'text-secondary', text: GROUP_TITLES[groupby] + ': '}),
                    el('span', {text: (entry.exclude ? 'not ' : '') + (entry.label || entry.key)}),
                    remover]));
            }));
            children.push(el('h5', {'class': 'mt-3', text: 'Filters'}));
            if(pending)
                children.push(el('p', {'class': 'small text-secondary',
                                       text: 'Release shift to apply'}));
            if(anyFilters)
                children.push(active, link('reset all', reset, {'class': 'dashboard-reset'}));
            else
                children.push(el('p', {'class': 'text-secondary', text: 'None'}));
            const help = el('details', {'class': 'mt-3 small'}, [
                el('summary', {text: 'How to filter'}),
                el('ul', {}, [
                    el('li', {text: 'Click a label to keep only its contributions, or again to undo'}),
                    el('li', {text: 'Ctrl-click a label to remove its contributions'}),
                    el('li', {text: 'Hold shift to pick several labels, applied when shift is released'}),
                    el('li', {text: 'Each chart is filtered by the others; its own selection is shaded'}),
                    el('li', {text: `Click the ${DRILL} before an assembly to show what is inside it; `
                                    + 'rows ending in ← are inside the row above'}),
                    el('li', {text: 'Drag across a chart to zoom, double-click to zoom out'})])]);
            children.push(help);
            info.replaceChildren(...children);
            if(window.MathJax && MathJax.typesetPromise)
                MathJax.typesetPromise([info]).catch(() => {});
        }

        function showNavigation(data){
            // only once drilled down
            const crumbs = data.breadcrumb.length > 1 ? data.breadcrumb : [];
            breadcrumb.replaceChildren(...crumbs.map((crumb, i) => {
                const last = i === crumbs.length - 1;
                return el('li', {'class': 'breadcrumb-item' + (last ? ' active' : '')},
                          [last ? el('span', {text: crumb.label})
                                : link(crumb.label, () => setRoot(crumb.key))]);
            }));
            // drill into a single selected assembly
            drill.replaceChildren();
            const selected = entries('component').filter(e => !e.exclude);
            const rows = data.charts.component || [];
            if(selected.length !== 1)
                return;
            const i = rows.findIndex(r => sameKey(r.key, selected[0].key));
            if(i >= 0 && rows[i].children)
                drill.appendChild(link(`${DRILL} Show inside ${rows[i].label}`,
                                       () => setRoot(rows[i].key),
                                       {'class': 'dashboard-drilldown btn btn-sm btn-outline-primary mb-1'}));
        }

        function draw(rescale){
            const mine = ++request;
            div.classList.add('loading');
            load().then(data => {
                if(mine !== request)
                    return;
                div.classList.remove('loading');
                current = data;
                const results = groupbys.map(groupby => ({
                    scalar: data.scalar, units: data.units,
                    rows: displayRows(data.charts[groupby] || [])}));
                const extent = budgetRange([].concat(...results.map(r => r.rows)));
                if(rescale){
                    fullRange = extent;
                    range = fullRange;
                } else if(range === fullRange){
                    fullRange = range = unionRange(fullRange, extent);
                } else {
                    fullRange = unionRange(fullRange, extent);
                }
                panels.forEach((panel, i) => {
                    const groupby = groupbys[i];
                    const own = entries(groupby).length > 0;
                    panel._rows = drawBudget(panel, results[i], threshold.value / 100,
                                             'By ' + GROUP_TITLES[groupby], range && range.slice(),
                                             own ? (r => r.selected) : null,
                                             groupby === 'component');
                    if(panel._rows.length && !panel._bound){
                        panel.on('plotly_relayout', event => sync(panel, event));
                        // zooming redraws the labels
                        panel.on('plotly_afterplot', () => titleDrill(panel));
                        panel._bound = true;
                    }
                });
                showInfo();
                showNavigation(data);
            }).catch(error => {
                if(mine !== request)
                    return;
                div.classList.remove('loading');
                panels.forEach(panel => showError(panel, error));
            });
        }

        function notify(){
            const json = JSON.stringify(filters);
            listeners.forEach(callback => callback(filters, json));
        }

        function update(){
            pending = false;
            const params = new URLSearchParams({scalar: select.value,
                                                filters: JSON.stringify(filters)});
            history.replaceState(null, '', '#' + params);
            draw(false);
            notify();
        }

        // filter by clicking the y axis labels, not the plot, which zooms
        panels.forEach((panel, i) => panel.addEventListener('click', event => {
            const tick = event.target.closest && event.target.closest('.ytick');
            const d = tick && tick.__data__;
            const row = d && panel._rows && panel._rows[Math.round(d.x)];
            if(!row)
                return;
            if(event.target.closest('[data-drill]') && row.children)
                setRoot(row.key);
            else
                pick(groupbys[i], row, event);
        }));

        document.addEventListener('keyup', event => {
            if(event.key === 'Shift' && pending)
                update();
        });
        select.addEventListener('change', () => {
            history.replaceState(null, '', '#' + new URLSearchParams(
                {scalar: select.value, filters: JSON.stringify(filters)}));
            draw(true);
        });
        threshold.addEventListener('change', () => draw(false));
        showInfo();
        whenVisible(div, () => draw(true));
        const controller = {
            onFilter(callback){
                listeners.push(callback);
                callback(filters, JSON.stringify(filters));
            },
        };
        if(options && options.table)
            filteredTable(options.table.id, options.table.url, controller);
        return controller;
    }

    /* Keep the contributions table in `div` filtered like `dash`, by
     * replacing it with the HTML from `url` for each change. The page
     * draws it unfiltered
     */
    function filteredTable(div, url, dash){
        if(typeof div === 'string')
            div = document.getElementById(div);
        let shown = JSON.stringify({root: []}), request = 0;
        dash.onFilter((filters, json) => {
            if(json === shown)
                return;
            shown = json;
            const mine = ++request;
            div.classList.add('loading');
            const sep = url.includes('?') ? '&' : '?';
            fetch(url + sep + new URLSearchParams({filters: json}))
                .then(response => response.text().then(text => {
                    if(!response.ok)
                        throw new Error(text || response.statusText);
                    return text;
                }))
                .then(html => {
                    if(mine !== request)
                        return;
                    div.innerHTML = html;
                    if(window.MathJax && MathJax.typesetPromise)
                        MathJax.typesetPromise([div]).catch(() => {});
                })
                .catch(error => {
                    if(mine !== request)
                        return;
                    shown = null;
                    div.replaceChildren(el('p', {'class': 'text-danger', text: error.message}));
                })
                .finally(() => {
                    if(mine === request)
                        div.classList.remove('loading');
                });
        });
    }

    return {spectrum: spectrum, spectrumBreakdown: spectrumBreakdown, dashboard: dashboard};
})();
