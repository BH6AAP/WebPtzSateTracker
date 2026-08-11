// 卫星跟踪前端逻辑
(function () {
  const $ = id => document.getElementById(id);
  let map = null;
  let currentNorad = null;
  let currentSatName = null; // 当前选中卫星名称
  let trackLayers = {};   // norad -> L.polyline
  let satPosMarkers = {}; // norad -> {circle, label}
  let currentMarker = null;
  let coverageLayer = null; // 卫星覆盖范围圆
  let tracking = false;
  let radarPoints = [];
  let radarTimer = null;
  let infoTimer = null;
  let obsMarker = null;

  // ===== 地图初始化 (本地地球纹理, 等距圆柱投影) =====
  function initMap() {
    const bounds = [[-90, -180], [90, 180]];
    map = L.map('map', {
      crs: L.CRS.EPSG4326,
      minZoom: 2, maxZoom: 6,
      zoomControl: false,
      maxBounds: bounds,
      maxBoundsViscosity: 1.0
    });
    // 缩放按钮放置右下角
    L.control.zoom({ position: 'bottomright' }).addTo(map);
    window.map = map;  // 暴露给其他脚本 (收起/展开时 invalidateSize)
    L.imageOverlay('earth_diffuse.jpg', bounds).addTo(map);
    // 以观测站为中心并显示观测站位置
    fetch('/api/sat/observer').then(r => r.json()).then(d => {
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
      }
    }).catch(() => { map.setView([20, 0], 2); });
  }

  // 保存观测站后刷新地图中心与标记
  async function updateObserverOnMap() {
    try {
      const r = await fetch('/api/sat/observer');
      const d = await r.json();
      if (d.ok) {
        map.setView([d.lat, d.lon], 3);
        if (obsMarker) map.removeLayer(obsMarker);
        obsMarker = L.circleMarker([d.lat, d.lon], {
          radius: 6, color: '#ef4444', fillColor: '#ef4444', fillOpacity: 1
        }).addTo(map).bindPopup('观测站');
      }
    } catch (e) { /* 忽略 */ }
  }
  window.updateObserverOnMap = updateObserverOnMap;

  // ===== 倒计时格式化 =====
  function formatCountdown(sec) {
    sec = Math.floor(sec);
    if (sec <= 0) return '正在过境';
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}小时${m}分`;
    if (m > 0) return `${m}分${s}秒`;
    return `${s}秒`;
  }

  // ===== 收藏列表 (按下次过境时间排序) =====
  async function loadFavorites() {
    try {
      const res = await fetch('/api/sat/favorites');
      const data = await res.json();
      if (!data.ok) return;
      const list = $('favList');
      list.innerHTML = '';
      const now = Date.now() / 1000;
      data.favorites.forEach(f => {
        const item = document.createElement('div');
        item.className = 'fav-item' + (f.norad === currentNorad ? ' active' : '');
        item.dataset.aos = f.next_aos || '';
        item.dataset.status = f.status || '';
        let infoHtml = '';
        if (f.status === 'in_pass') {
          infoHtml = `<span class="countdown in-pass">正在过境</span> · 最大仰角 ${f.max_el}°`;
        } else if (f.status === 'upcoming' && f.next_aos) {
          const sec = Math.max(0, f.next_aos - now);
          const urgentClass = sec < 1800 ? ' urgent' : '';
          infoHtml = `<span class="countdown${urgentClass}">${formatCountdown(sec)}后过境</span> · 最大仰角 ${f.max_el}°`;
        } else {
          infoHtml = `<span class="countdown">未来48小时无过境</span>`;
        }
        item.innerHTML = `<div><div class="name">${f.name || f.norad}</div><div class="norad">NORAD ${f.norad}</div><div class="pass-info">${infoHtml}</div></div>
          <button class="del" data-norad="${f.norad}">删除</button>`;
        item.addEventListener('click', (e) => {
          if (e.target.classList.contains('del')) return;
          selectSatellite(f.norad, f.name || f.norad);
        });
        item.querySelector('.del').addEventListener('click', async (e) => {
          e.stopPropagation();
          await fetch('/api/sat/favorites/' + f.norad, { method: 'DELETE' });
          delete trackLayers[f.norad];
          if (satPosMarkers[f.norad]) {
            map.removeLayer(satPosMarkers[f.norad].circle);
            map.removeLayer(satPosMarkers[f.norad].label);
            delete satPosMarkers[f.norad];
          }
          if (f.norad === currentNorad) { currentNorad = null; clearSatellite(); }
          loadFavorites();
        });
        list.appendChild(item);
      });
      // 未选中卫星: 显示实时位置 (圆圈+名称); 选中卫星: 显示轨迹
      data.favorites.forEach(f => {
        if (f.norad === currentNorad) loadTrack(f.norad, trackHours);
        else loadSatPos(f.norad, f.name || f.norad);
      });
    } catch (e) { /* 忽略 */ }
  }

  // 每秒更新收藏列表倒计时
  setInterval(() => {
    const now = Date.now() / 1000;
    document.querySelectorAll('.fav-item').forEach(item => {
      const el = item.querySelector('.countdown');
      if (!el) return;
      const status = item.dataset.status;
      const aos = parseFloat(item.dataset.aos);
      if (status === 'in_pass') {
        el.textContent = '正在过境';
        el.className = 'countdown in-pass';
      } else if (status === 'upcoming' && aos) {
        const sec = Math.max(0, aos - now);
        el.textContent = formatCountdown(sec) + '后过境';
        el.className = 'countdown' + (sec < 1800 ? ' urgent' : '');
      }
    });
  }, 1000);

  // ===== 未选中卫星实时位置 (圆圈 + 名称) =====
  async function loadSatPos(norad, name) {
    try {
      const res = await fetch('/api/sat/position/' + norad);
      const data = await res.json();
      if (!data.ok) return;
      if (satPosMarkers[norad]) {
        map.removeLayer(satPosMarkers[norad].circle);
        map.removeLayer(satPosMarkers[norad].label);
      }
      const circle = L.circleMarker([data.sub_lat, data.sub_lon], {
        radius: 5, color: '#facc15', fillColor: '#facc15', fillOpacity: 0.8
      }).addTo(map);
      const label = L.marker([data.sub_lat, data.sub_lon], {
        icon: L.divIcon({ className: 'sat-label', html: name, iconSize: [90, 16] })
      }).addTo(map);
      satPosMarkers[norad] = { circle, label };
    } catch (e) { /* 忽略 */ }
  }

  // ===== 添加收藏 =====
  $('btnFavAdd').onclick = async () => {
    const q = $('favSearch').value.trim();
    if (!q) { toast('请输入搜索词'); return; }
    try {
      // 后端检索: 名称/编号包含匹配, 最多返回 20 条, 无需拉全量 TLE
      const res = await fetch('/api/sat/search?q=' + encodeURIComponent(q));
      const data = await res.json();
      if (!data.ok) return;
      const matches = data.satellites;
      if (matches.length === 0) { toast('未找到卫星'); return; }
      if (matches.length === 1) {
        await addFav(matches[0].norad);
      } else {
        // 多个匹配, 用第一个
        await addFav(matches[0].norad);
        toast(`找到 ${matches.length} 个, 已添加第一个`);
      }
    } catch (e) { toast('搜索失败'); }
  };
  async function addFav(norad) {
    try {
      const res = await fetch('/api/sat/favorites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ norad }) });
      const data = await res.json();
      if (data.ok) { toast('已添加收藏'); loadFavorites(); }
      else toast('添加失败: ' + (data.detail || ''));
    } catch (e) { toast('添加失败'); }
  }

  // ===== 加载卫星轨迹 (星下点) =====
  async function loadTrack(norad, hours) {
    try {
      const res = await fetch('/api/sat/track/' + norad + '?hours=' + (hours || 24) + '&step=120');
      const data = await res.json();
      if (!data.ok || !data.points || data.points.length === 0) return;
      const latlngs = data.points.map(p => [p.lat, p.lon]);
      if (trackLayers[norad]) map.removeLayer(trackLayers[norad]);
      trackLayers[norad] = L.polyline(latlngs, { color: '#0ea5e9', weight: 2, opacity: 0.8 }).addTo(map);
    } catch (e) { /* 忽略 */ }
  }

  // ===== 轨迹时长切换 (1h/3h/6h/12h) =====
  let trackHours = 12;  // 地图轨迹默认时长
  function bindTrackDurBtns() {
    document.querySelectorAll('#durBtns .dur-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        trackHours = parseInt(btn.dataset.hours, 10);
        document.querySelectorAll('#durBtns .dur-btn').forEach(b => b.classList.toggle('active', b === btn));
        if (currentNorad) loadTrack(currentNorad, trackHours);
      });
    });
  }

  // ===== 选择卫星 =====
  function clearAllTracks() {
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
  }
  async function selectSatellite(norad, name) {
    // 切换卫星时移除旧轨迹
    clearAllTracks();
    currentNorad = norad;
    currentSatName = name || norad;
    // 高亮列表
    document.querySelectorAll('.fav-item').forEach(el => el.classList.remove('active'));
    const items = document.querySelectorAll('.fav-item');
    items.forEach(el => {
      if (el.querySelector('.del').dataset.norad === norad) el.classList.add('active');
    });
    // 移除该卫星的实时位置圆圈, 显示 24 小时轨迹
    if (satPosMarkers[norad]) {
      map.removeLayer(satPosMarkers[norad].circle);
      map.removeLayer(satPosMarkers[norad].label);
      delete satPosMarkers[norad];
    }
    loadTrack(norad, trackHours);
    loadRadar(norad);
    loadPasses(norad);
    startInfoPoll(norad);
  }

  function clearSatellite() {
    currentNorad = null;
    currentSatName = null;
    $('satInfo').innerHTML = '<div class="row"><span class="k">未选择卫星</span></div>';
    if (currentMarker) { map.removeLayer(currentMarker); currentMarker = null; }
    if (coverageLayer) { map.removeLayer(coverageLayer); coverageLayer = null; }
    if (radarTimer) clearInterval(radarTimer);
    if (infoTimer) clearInterval(infoTimer);
    $('passList').innerHTML = '';
    drawRadar([]);
    // 移除所有轨迹, 恢复实时位置圆圈
    Object.keys(trackLayers).forEach(k => { if (trackLayers[k]) map.removeLayer(trackLayers[k]); });
    trackLayers = {};
    loadFavorites();
  }

  // 卫星覆盖圆半径: 仰角>=0 的地面范围 (米)
  function coverageRadius(altKm) {
    const R = 6371;
    const h = Math.max(altKm, 100);
    const gamma = Math.acos(R / (R + h)); // 地心角 rad
    return gamma * R * 1000;
  }
  // 绘制/更新覆盖圆 (圆心跟随星下点)
  function drawCoverage(subLat, subLon, altKm) {
    const latlng = [subLat, subLon];
    if (coverageLayer) { coverageLayer.setLatLng(latlng); return; }
    coverageLayer = L.circle(latlng, {
      radius: coverageRadius(altKm),
      color: '#0ea5e9', weight: 1,
      fillColor: '#0ea5e9', fillOpacity: 0.08
    }).addTo(map);
  }

  // ===== 卫星信息轮询 =====
  function startInfoPoll(norad) {
    if (infoTimer) clearInterval(infoTimer);
    const update = async () => {
      try {
        const res = await fetch('/api/sat/position/' + norad);
        const data = await res.json();
        if (!data.ok) return;
        $('satInfo').innerHTML = `
          <div class="row"><span class="k">方位角</span><span class="v">${data.azimuth}°</span></div>
          <div class="row"><span class="k">仰角</span><span class="v">${data.elevation}°</span></div>
          <div class="row"><span class="k">距离</span><span class="v">${data.distance} km</span></div>
          <div class="row"><span class="k">可见</span><span class="v" style="color:${data.visible ? '#4ade80' : '#ef4444'}">${data.visible ? '是' : '否'}</span></div>`;
        // 覆盖范围圆 (跟随星下点)
        drawCoverage(data.sub_lat, data.sub_lon, data.alt_km);
        // 地图上当前星下点 (黄色 + 卫星名称标签)
        if (currentMarker) map.removeLayer(currentMarker);
        const labelText = currentSatName || '卫星';
        currentMarker = L.layerGroup([
          L.circleMarker([data.sub_lat, data.sub_lon], { radius: 7, color: '#facc15', fillColor: '#facc15', fillOpacity: 1 }),
          L.marker([data.sub_lat, data.sub_lon], {
            icon: L.divIcon({ className: 'sat-label', html: labelText, iconSize: [100, 16] })
          })
        ]).addTo(map);
        // 雷达图当前点
        drawRadar(radarPoints, { az: data.azimuth, el: data.elevation });
      } catch (e) { /* 忽略 */ }
    };
    update();
    infoTimer = setInterval(update, 1000);
  }

  // ===== 过境列表 =====
  async function loadPasses(norad) {
    try {
      const res = await fetch('/api/sat/passes/' + norad + '?hours=24&min_elev=0');
      const data = await res.json();
      if (!data.ok) return;
      const list = $('passList');
      list.innerHTML = '<div style="font-size:13px;color:#7dd3fc;margin-bottom:8px">未来过境</div>';
      data.passes.slice(0, 5).forEach(p => {
        const aos = new Date(p.aos * 1000);
        const los = new Date(p.los * 1000);
        const fmt = d => d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        list.innerHTML += `<div class="pass-item">
          <div>${fmt(aos)} - ${fmt(los)}</div>
          <div class="t">最大仰角 ${p.max_el}° · 方位 ${p.aos_az}°→${p.los_az}° · ${Math.round(p.duration / 60)}分</div></div>`;
      });
    } catch (e) { /* 忽略 */ }
  }

  // ===== 雷达图 =====
  async function loadRadar(norad) {
    try {
      const res = await fetch('/api/sat/radar/' + norad + '?minutes=30&step=30');
      const data = await res.json();
      if (!data.ok) return;
      radarPoints = data.points;
      drawRadar(radarPoints);
    } catch (e) { /* 忽略 */ }
  }

  function drawRadar(points, current) {
    const canvas = $('radar');
    const ctx = canvas.getContext('2d');
    const W = canvas.width, H = canvas.height;
    const cx = W / 2, cy = H / 2, R = Math.min(W, H) / 2 - 12;
    ctx.clearRect(0, 0, W, H);
    // 网格
    ctx.strokeStyle = '#334155';
    ctx.lineWidth = 1;
    [1 / 3, 2 / 3, 1].forEach(f => {
      ctx.beginPath();
      ctx.arc(cx, cy, R * f, 0, Math.PI * 2);
      ctx.stroke();
    });
    ctx.beginPath();
    ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
    ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
    ctx.stroke();
    // 方位标注
    ctx.fillStyle = '#94a3b8';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('N', cx, cy - R - 4);
    ctx.fillText('E', cx + R + 10, cy + 4);
    ctx.fillText('S', cx, cy + R + 14);
    ctx.fillText('W', cx - R - 10, cy + 4);
    // 轨迹
    if (points && points.length > 1) {
      ctx.strokeStyle = '#facc15';
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      points.forEach(p => {
        if (p.el < 0) { started = false; return; }
        const r = R * (1 - p.el / 90);
        const x = cx + r * Math.sin(p.az * Math.PI / 180);
        const y = cy - r * Math.cos(p.az * Math.PI / 180);
        if (!started) { ctx.moveTo(x, y); started = true; }
        else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    // 当前卫星位置
    if (current) {
      const r = R * (1 - current.el / 90);
      const x = cx + r * Math.sin(current.az * Math.PI / 180);
      const y = cy - r * Math.cos(current.az * Math.PI / 180);
      ctx.fillStyle = '#0ea5e9';
      ctx.beginPath();
      ctx.arc(x, y, 5, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  // ===== AS5600 标定 =====
  const encCalPoints = [];
  let encLatest = null;

  function renderEncCal() {
    $('encCalCount').textContent = encCalPoints.length;
    const box = $('encCalPoints');
    if (!encCalPoints.length) {
      box.innerHTML = '<div style="text-align:center;padding:8px 0;">暂无</div>';
      return;
    }
    box.innerHTML = encCalPoints.map((p, i) =>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;border-bottom:1px solid #1e293b;">
        <span>#${i + 1} AS:${p.as5600_angle.toFixed(1)}° → 真实:${p.pan.toFixed(1)}°</span>
        <button data-idx="${i}" style="padding:2px 6px;border:none;border-radius:4px;background:#64748b;color:#fff;font-size:11px;cursor:pointer;">删除</button>
      </div>`).join('');
    box.querySelectorAll('button').forEach(b => {
      b.onclick = () => { encCalPoints.splice(parseInt(b.dataset.idx), 1); renderEncCal(); };
    });
  }

  function updateEncLatest(latest) {
    encLatest = latest || null;
    if (!latest) return;
    $('encCalRaw').textContent = latest.raw ?? '-';
    $('encCalAngle').textContent = latest.angle !== null && latest.angle !== undefined ? latest.angle.toFixed(1) : '-';
    $('encCalPan').textContent = latest.pan !== null && latest.pan !== undefined ? latest.pan.toFixed(1) : '-';
  }

  async function loadEncCal() {
    try {
      const r = await fetch('/api/encoder/calibrate');
      const d = await r.json();
      if (!d.ok) return;
      encCalPoints.length = 0;
      if (d.cal && d.cal.points) {
        d.cal.points.forEach(p => encCalPoints.push({ as5600_angle: p.as5600_angle, pan: p.pan }));
      }
      renderEncCal();
      updateEncLatest(d.latest);
    } catch (e) { /* 忽略 */ }
  }

  async function pollEncoderCal() {
    try {
      const r = await fetch('/api/encoder/calibrate');
      const d = await r.json();
      if (d.ok) updateEncLatest(d.latest);
    } catch (e) { /* 忽略 */ }
  }

  $('encCalToggle').onclick = () => {
    $('encCalBody').classList.toggle('hidden');
    $('encCalToggle').classList.toggle('collapsed');
  };
  $('btnEncCalAdd').onclick = () => {
    if (!encLatest || encLatest.angle === null || encLatest.angle === undefined) {
      toast('尚未收到 AS5600 数据');
      return;
    }
    const real = parseFloat($('encCalReal').value);
    if (isNaN(real)) { toast('请输入云台真实水平角'); return; }
    encCalPoints.push({ as5600_angle: encLatest.angle, pan: real });
    renderEncCal();
    toast('已记录标定点');
  };
  $('btnEncCalClear').onclick = () => {
    encCalPoints.length = 0;
    renderEncCal();
    $('encCalResult').textContent = '';
  };
  $('btnEncCalSave').onclick = async () => {
    if (encCalPoints.length < 1) { toast('请至少记录 1 个点'); return; }
    try {
      const r = await fetch('/api/encoder/calibrate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ points: encCalPoints })
      });
      const d = await r.json();
      if (d.ok) {
        $('encCalResult').textContent = '标定已保存' + (d.mapped_pan !== null && d.mapped_pan !== undefined ? `，当前映射 pan ${d.mapped_pan.toFixed(1)}°` : '');
        toast('标定保存成功');
      } else {
        toast('保存失败: ' + (d.detail || ''));
      }
    } catch (e) { toast('保存失败: ' + e.message); }
  };

  // ===== 云台跟踪 =====
  $('btnTrack').onclick = async () => {
    if (!currentNorad) { toast('请先选择卫星'); return; }
    if (!tracking) {
      try {
        const res = await fetch('/api/sat/track/' + currentNorad, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        const data = await res.json();
        if (data.ok) {
          tracking = true;
          $('btnTrack').textContent = '停止跟踪';
          $('btnTrack').classList.add('tracking');
          toast('开始跟踪 ' + currentNorad);
        } else toast('跟踪失败: ' + (data.detail || ''));
      } catch (e) { toast('跟踪失败'); }
    } else {
      try {
        await fetch('/api/sat/track/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
        tracking = false;
        $('btnTrack').textContent = '开始跟踪';
        $('btnTrack').classList.remove('tracking');
        toast('已停止跟踪');
      } catch (e) { toast('停止失败'); }
    }
  };

  // ===== 初始化 =====
  initMap();
  bindTrackDurBtns();
  loadFavorites();
  loadEncCal();
  setInterval(loadFavorites, 30000);
  setInterval(pollEncoderCal, 500);
})();
