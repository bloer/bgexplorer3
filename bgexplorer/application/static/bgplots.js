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

    /* Step-line traces with a shaded error band for one histogram */
    function spectrumTraces(name, h, color, logy){
        // repeat the last value so the final bin is drawn to its high edge
        const x = h.bins;
        const pad = arr => arr.concat(arr.length ? [arr[arr.length-1]] : []);
        const y = pad(h.value);
        const smallest = Math.min(...h.value.filter(v => v > 0));
        const floor = (logy && isFinite(smallest)) ? smallest / 100 : null;
        const low = pad(h.value.map((v, i) => {
            const lo = v - h.err_minus[i];
            return (logy && !(lo > 0)) ? floor : lo;
        }));
        const high = pad(h.value.map((v, i) => v + h.err_plus[i]));
        const common = {x: x, mode: 'lines', line: {shape: 'hv', width: 0},
                        legendgroup: name, showlegend: false, hoverinfo: 'skip'};
        return [
            Object.assign({}, common, {y: low}),
            Object.assign({}, common, {y: high, fill: 'tonexty',
                                       fillcolor: color + '40'}),
            {x: x, y: y, name: name, legendgroup: name, mode: 'lines',
             line: {shape: 'hv', color: color, width: 1.5}},
        ];
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
     * bars, upper limits as blue left-pointing arrows from the 90% limit
     */
    function budgetTraces(rows){
        const measured = rows.filter(r => r.measured && r.measured.value > 0);
        const limits = rows.filter(r => r.limit && r.limit.upper_limit > 0);
        const arrowx = [], arrowy = [];
        limits.forEach(r => {
            arrowx.push(r.limit.upper_limit, r.limit.upper_limit / 4, null);
            arrowy.push(r.label, r.label, null);
        });
        return [
            {type: 'scatter', mode: 'lines', x: arrowx, y: arrowy,
             line: {color: LIMIT_COLOR, width: 2}, hoverinfo: 'skip',
             showlegend: false, legendgroup: 'limit'},
            {type: 'scatter', mode: 'markers', name: '90% upper limit',
             legendgroup: 'limit',
             x: limits.map(r => r.limit.upper_limit / 4),
             y: limits.map(r => r.label),
             customdata: limits.map(r => r.limit.upper_limit),
             hovertemplate: '%{y}: < %{customdata:.3g}<extra></extra>',
             marker: {symbol: 'triangle-left', size: 10, color: LIMIT_COLOR}},
            {type: 'scatter', mode: 'markers', name: 'Measured',
             x: measured.map(r => r.measured.value),
             y: measured.map(r => r.label),
             error_x: {type: 'data', symmetric: false,
                       array: measured.map(r => r.measured.err_plus),
                       // keep the lower bar on a log axis
                       arrayminus: measured.map(r => Math.min(r.measured.err_minus,
                                                              0.999 * r.measured.value)),
                       color: MEASURED_COLOR},
             hovertemplate: '%{y}: %{x:.3g}<extra></extra>',
             marker: {symbol: 'circle', size: 8, color: MEASURED_COLOR}},
        ];
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

    function drawBudget(plot, data, threshold, title, range){
        const total = data.rows.reduce((sum, r) => sum + rowSize(r), 0);
        const rows = data.rows.filter(r => rowSize(r) > 0
                                      && rowSize(r) >= threshold * total);
        // clear plotly's state along with any message
        Plotly.purge(plot);
        plot.replaceChildren();
        if(!rows.length){
            plot.appendChild(el('p', {'class': 'text-secondary', text: 'No contributions'}));
            return false;
        }
        const layout = {
            title: {text: title, font: {size: 14}},
            margin: {t: 30, r: 10, l: 10},
            height: 120 + 22 * rows.length,
            xaxis: {type: 'log', title: {text: data.scalar + unitLabel(data.units)},
                    exponentformat: 'power', gridcolor: '#ddd', range: range},
            yaxis: {type: 'category', automargin: true, autorange: 'reversed',
                    categoryorder: 'array', categoryarray: rows.map(r => r.label)},
            legend: {orientation: 'h', y: -0.15},
            showlegend: false,
        };
        Plotly.react(plot, budgetTraces(rows), layout, CONFIG);
        return true;
    }

    /* Budget breakdowns of one component, one panel per groupby. `url` is
     * the budget.json endpoint, `scalars` the result names to choose from.
     * All panels share one x range, covering every row before the
     * threshold hides any, and zoom and pan together.
     */
    function budget(div, url, groupbys, scalars){
        if(typeof div === 'string')
            div = document.getElementById(div);
        const id = div.id || 'budget';
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
        const panels = groupbys.map(groupby => el('div', {'class': 'col-lg-4 bgplot',
                                                          'data-groupby': groupby}));
        div.replaceChildren(controls, el('div', {'class': 'row'}, panels));
        div.panels = panels;
        const cache = {};
        // the shared range: the full extent of the current scalar, or a zoom
        let fullRange = null, range = null, syncing = false;

        function load(scalar){
            if(!cache[scalar]){
                const sep = url.includes('?') ? '&' : '?';
                cache[scalar] = Promise.all(groupbys.map(groupby => getJSON(
                    url + sep + new URLSearchParams({scalar: scalar, groupby: groupby}))));
            }
            return cache[scalar];
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

        function draw(rescale){
            load(select.value).then(results => {
                if(rescale){
                    fullRange = budgetRange([].concat(...results.map(data => data.rows)));
                    range = fullRange;
                }
                panels.forEach((panel, i) => {
                    if(drawBudget(panel, results[i], threshold.value / 100,
                                  'By ' + groupbys[i], range && range.slice()))
                        panel.on('plotly_relayout', event => sync(panel, event));
                });
            }).catch(error => panels.forEach(panel => showError(panel, error)));
        }
        select.addEventListener('change', () => draw(true));
        threshold.addEventListener('change', () => draw(false));
        whenVisible(div, () => draw(true));
    }

    return {spectrum: spectrum, budget: budget};
})();
