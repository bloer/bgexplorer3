/* Interactive plots for Background Explorer, drawn with plotly.js from the
 * JSON produced by application/plotting.py
 */
const bgplots = (function(){
    "use strict";

    const COLORS = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'];
    const MEASURED_COLOR = '#d62728';
    const LIMIT_COLOR = '#1f4fd6';
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

    /* Traces for one histogram: a step line with a shaded error band for
     * measured bins, and a dashed step at the 90% upper limit with a
     * down-pointing marker for upper limit bins
     */
    function spectrumTraces(name, h, color, logy){
        const limit = i => h.is_limit && h.is_limit[i];
        const measured = i => !limit(i);
        const smallest = Math.min(...h.value.filter(v => v > 0));
        const floor = (logy && isFinite(smallest)) ? smallest / 100 : null;
        const [x, y] = steps(h, measured, i => h.value[i]);
        const [, low] = steps(h, measured, i => {
            const lo = h.value[i] - h.err_minus[i];
            return (logy && !(lo > 0)) ? floor : lo;
        });
        const [, high] = steps(h, measured, i => h.value[i] + h.err_plus[i]);
        // the band: each run of bins as a closed polygon, high then low
        const bandx = [], bandy = [];
        let start = 0;
        for(let j = 0; j <= x.length; ++j){
            if(j < x.length && x[j] !== null)
                continue;
            if(j > start){
                bandx.push(...x.slice(start, j), ...x.slice(start, j).reverse(), null);
                bandy.push(...high.slice(start, j), ...low.slice(start, j).reverse(), null);
            }
            start = j + 1;
        }
        const traces = [
            {x: bandx, y: bandy, mode: 'lines', fill: 'toself', fillcolor: color + '40',
             line: {width: 0}, legendgroup: name, showlegend: false, hoverinfo: 'skip'},
            {x: x, y: y, name: name, legendgroup: name, mode: 'lines', connectgaps: false,
             line: {color: color, width: 1.5}},
        ];
        const limits = h.value.map((v, i) => i).filter(limit);
        if(limits.length){
            const [ulx, uly] = steps(h, limit, i => h.upper_limit[i]);
            traces.push(
                {x: ulx, y: uly, mode: 'lines', connectgaps: false, legendgroup: name,
                 showlegend: false, hoverinfo: 'skip',
                 line: {color: color, width: 1, dash: 'dash'}},
                {x: limits.map(i => (h.bins[i] + h.bins[i+1]) / 2),
                 y: limits.map(i => h.upper_limit[i]),
                 mode: 'markers', legendgroup: name, showlegend: false,
                 name: name + ' upper limits',
                 hovertemplate: '%{x}: < %{y:.3g} (90% UL)<extra>' + name + '</extra>',
                 marker: {symbol: 'triangle-down', size: 9, color: color}});
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
            const traces = [];
            selected.forEach((name, i) => traces.push(
                ...spectrumTraces(name, data[name], COLORS[i % COLORS.length], logy.checked)));
            const first = data[selected[0]] || {};
            const layout = {
                height: SPECTRUM_HEIGHT,
                margin: {t: 20, r: 10},
                xaxis: {title: {text: 'Energy' + unitLabel(first.binsunit)},
                        type: logx.checked ? 'log' : 'linear'},
                yaxis: {title: {text: unitLabel(first.units).trim()},
                        type: logy.checked ? 'log' : 'linear', exponentformat: 'power'},
                showlegend: selected.length > 1,
            };
            Plotly.react(plot, traces, layout, CONFIG);
        }
        [select, logx, logy].forEach(input => input.addEventListener('change', draw));
        draw();
    }

    /* A horizontal budget chart: measured values as red points with error
     * bars, upper limits as blue left-pointing arrows from the 90% limit.
     * Rows where `dim(row)` are drawn faded. The measured points of the
     * other rows are the last trace.
     */
    /* Traces for budget `rows`, each with a unique `id` for its y category.
     * Rows where `dim` is true are faded. Always the same four traces, so
     * plotly can animate between filters: faded and unfaded limits, then
     * faded and unfaded measured values
     */
    function budgetTraces(rows, dim, units){
        dim = dim || (() => false);
        const u = units ? ' ' + units : '';
        const measured = rows.filter(r => r.measured && r.measured.value > 0);
        const limits = rows.filter(r => r.limit && r.limit.upper_limit > 0);
        // a limit is an arrow from the limit to a quarter of it
        const limitTraces = [true, false].map(faded => {
            const rows = limits.filter(r => dim(r) === faded);
            return {type: 'scatter', mode: 'markers', name: '90% upper limit',
                    opacity: faded ? 0.25 : 1, showlegend: !faded,
                    ids: rows.map(r => r.id),
                    x: rows.map(r => r.limit.upper_limit / 4),
                    y: rows.map(r => r.id),
                    customdata: rows.map(r => r.limit.upper_limit),
                    error_x: {type: 'data', symmetric: false, width: 0, thickness: 2,
                              array: rows.map(r => 0.75 * r.limit.upper_limit),
                              arrayminus: rows.map(() => 0), color: LIMIT_COLOR},
                    hovertemplate: `< %{customdata:.3g}${u}<extra></extra>`,
                    marker: {symbol: 'triangle-left', size: 10, color: LIMIT_COLOR}};
        });
        const measuredTraces = [true, false].map(faded => {
            const rows = measured.filter(r => dim(r) === faded);
            return {type: 'scatter', mode: 'markers', name: 'Measured',
                    opacity: faded ? 0.25 : 1, showlegend: !faded,
                    ids: rows.map(r => r.id),
                    x: rows.map(r => r.measured.value),
                    y: rows.map(r => r.id),
                    customdata: rows.map(r => [r.measured.err_plus, r.measured.err_minus]),
                    error_x: {type: 'data', symmetric: false,
                              array: rows.map(r => r.measured.err_plus),
                              // keep the lower bar on a log axis
                              arrayminus: rows.map(r => Math.min(r.measured.err_minus,
                                                                 0.999 * r.measured.value)),
                              color: MEASURED_COLOR},
                    hovertemplate: '%{x:.3g} (+%{customdata[0]:.2g} −%{customdata[1]:.2g})'
                                 + `${u}<extra></extra>`,
                    marker: {symbol: 'circle', size: 8, color: MEASURED_COLOR}};
        });
        return limitTraces.concat(measuredTraces);
    }

    function rowSize(row){
        return Math.max(row.measured ? row.measured.value : 0,
                        row.limit ? row.limit.upper_limit : 0);
    }

    /* log10 x range covering everything drawn for `rows`, padded */
    function budgetRange(rows){
        const points = [];
        rows.forEach(r => {
            if(r.measured && r.measured.value > 0){
                const m = r.measured;
                points.push(m.value, m.value + m.err_plus);
                if(m.value - m.err_minus > 0)
                    points.push(m.value - m.err_minus);
            }
            if(r.limit && r.limit.upper_limit > 0)
                points.push(r.limit.upper_limit, r.limit.upper_limit / 4);
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
        const empty = !(row.measured || row.limit);
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
        const size = r => r.size === undefined ? rowSize(r) : r.size;
        const total = data.rows.filter(r => !r.depth)
                               .reduce((sum, r) => sum + size(r), 0);
        const rows = data.rows.filter(r => size(r) > 0 && size(r) >= threshold * total);
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
            yaxis: {type: 'category', automargin: true, autorange: 'reversed',
                    fixedrange: true, categoryorder: 'array', categoryarray: ids,
                    tickmode: 'array', tickvals: ids,
                    ticktext: rows.map(r => tickText(r, highlight && highlight(r), drillable))},
            hovermode: 'closest',
            shapes: shapes,
            legend: {orientation: 'h', y: -0.15},
            showlegend: false,
        };
        const traces = budgetTraces(rows, r => r.selected === false, data.units);
        if(same){
            // relabel, then move the values from where they were
            Plotly.react(plot, plot.data, layout, CONFIG).then(() => {
                titleDrill(plot);
                return Plotly.animate(
                    plot, {data: traces, traces: traces.map((t, i) => i)},
                    {transition: {duration: 400, easing: 'cubic-in-out'},
                     frame: {duration: 400, redraw: false}, mode: 'immediate'});
            });
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
     */
    function dashboard(div, url, groupbys, scalars){
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
            const total = (title, t, cls) => {
                children.push(el('p', {'class': 'mb-0 ' + cls}, [
                    el('strong', {text: title + ': '}),
                    el('span', {text: formatValue(t.all, current.units)})]));
                children.push(el('p', {'class': 'mb-2 small text-secondary', text:
                    t.measured && t.limit ? `measured ${formatValue(t.measured, current.units)}, `
                                          + `limits ${formatValue(t.limit, current.units)}` : ''}));
            };
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
                    el('span', {text: (entry.exclude ? 'not ' : '') + entry.label}),
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

        function update(){
            pending = false;
            const params = new URLSearchParams({scalar: select.value,
                                                filters: JSON.stringify(filters)});
            history.replaceState(null, '', '#' + params);
            draw(false);
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
    }

    return {spectrum: spectrum, dashboard: dashboard};
})();
