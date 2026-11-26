/* Formulario propio del recuperador (v1).
   El bridge sirve este archivo con la configuración del formulario ya inyectada
   en el lugar del marcador de abajo. Sin dependencias, sin eval, y sin innerHTML
   con datos de la configuración: todo se arma con createElement y textContent. */
(function () {
  'use strict';

  var CONFIG = /*__SF_CONFIG__*/null;

  // Se captura al inicio: dentro de un callback ya no apunta a este script.
  var scriptActual = document.currentScript;

  if (!CONFIG || typeof CONFIG !== 'object' || typeof CONFIG.clave !== 'string' || !CONFIG.clave) {
    return;
  }

  var clave = CONFIG.clave;
  var etiquetas = CONFIG.etiquetas || {};
  var paises = Array.isArray(CONFIG.paises) ? CONFIG.paises : [];
  var links = Array.isArray(CONFIG.links) ? CONFIG.links : [];

  window.setterForms = window.setterForms || {};
  window.setterForms[clave] = { version: 1 };

  var MENSAJES = {
    nombre: 'Escribe tu nombre completo',
    email: 'Revisa tu correo',
    telefono: 'Revisa tu número de WhatsApp',
    datos: 'Revisa tus datos e intenta de nuevo',
    demasiados: 'Demasiados intentos. Espera unos minutos e intenta de nuevo.',
    red: 'No pudimos enviar tus datos. Intenta de nuevo en un momento.',
    listo: '¡Listo! Recibimos tus datos.',
    enviando: 'Enviando…'
  };

  var contador = 0;

  function idUnico(sufijo) {
    var base = 'sf-' + clave.replace(/[^A-Za-z0-9_-]/g, '-') + '-' + sufijo;
    var id;
    do {
      contador += 1;
      id = base + '-' + contador;
    } while (document.getElementById(id));
    return id;
  }

  function el(tag, clase) {
    var nodo = document.createElement(tag);
    if (clase) nodo.className = clase;
    return nodo;
  }

  // Solo se aceptan links http(s), mailto o relativos; cualquier otro esquema
  // (javascript:, data:...) se dibuja como texto sin link.
  function urlSegura(url) {
    if (typeof url !== 'string' || !url) return null;
    try {
      var u = new URL(url, location.href);
      if (u.protocol === 'http:' || u.protocol === 'https:' || u.protocol === 'mailto:') return u.href;
    } catch (e) {
      return null;
    }
    return null;
  }

  function crearLink(texto, url) {
    var href = urlSegura(url);
    if (!href) return document.createTextNode(texto);
    var a = document.createElement('a');
    a.href = href;
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = texto;
    return a;
  }

  function inyectarCss() {
    if (typeof CONFIG.css !== 'string' || !CONFIG.css) return;
    if (document.querySelector('style[data-sf-css]')) return;
    var estilo = document.createElement('style');
    estilo.setAttribute('data-sf-css', '');
    estilo.textContent = CONFIG.css;
    (document.head || document.documentElement).appendChild(estilo);
  }

  function armarAclaracion() {
    var texto = typeof CONFIG.aclaracion === 'string' ? CONFIG.aclaracion : '';
    if (!texto && !links.length) return null;
    var p = el('p', 'sf-aclaracion');
    // Segmentos: strings sueltos o {link}. Cada link reemplaza la primera
    // aparición literal de su texto dentro de un segmento de texto.
    var segmentos = [texto];
    var sobrantes = [];
    links.forEach(function (link) {
      if (!link || typeof link.texto !== 'string' || !link.texto) {
        if (link && typeof link.url === 'string' && link.url) sobrantes.push({ texto: link.url, url: link.url });
        return;
      }
      for (var i = 0; i < segmentos.length; i++) {
        var seg = segmentos[i];
        if (typeof seg !== 'string') continue;
        var pos = seg.indexOf(link.texto);
        if (pos === -1) continue;
        var nuevos = [];
        if (pos > 0) nuevos.push(seg.slice(0, pos));
        nuevos.push({ texto: link.texto, url: link.url });
        var resto = seg.slice(pos + link.texto.length);
        if (resto) nuevos.push(resto);
        segmentos.splice.apply(segmentos, [i, 1].concat(nuevos));
        return;
      }
      sobrantes.push(link);
    });
    segmentos.forEach(function (seg) {
      if (typeof seg === 'string') p.appendChild(document.createTextNode(seg));
      else p.appendChild(crearLink(seg.texto, seg.url));
    });
    sobrantes.forEach(function (link, i) {
      var sep = (p.childNodes.length || i > 0) ? ' ' : '';
      if (sep) p.appendChild(document.createTextNode(sep));
      p.appendChild(crearLink(link.texto, link.url));
    });
    return p;
  }

  function leerCookie(nombre) {
    var partes = (document.cookie || '').split(';');
    for (var i = 0; i < partes.length; i++) {
      var par = partes[i].replace(/^\s+/, '');
      if (par.indexOf(nombre + '=') === 0) {
        try {
          return decodeURIComponent(par.slice(nombre.length + 1));
        } catch (e) {
          return par.slice(nombre.length + 1);
        }
      }
    }
    return '';
  }

  // Arma el teléfono en formato +<prefijo><número>. Si lo escrito ya trae el
  // prefijo del país y es largo (más de 10 dígitos), se usa tal cual.
  function normalizarTelefono(crudo, prefijo) {
    var digitos = String(crudo || '').replace(/\D/g, '');
    if (digitos.charAt(0) === '0') digitos = digitos.slice(1);
    if (!prefijo) return '+' + digitos;
    if (digitos.indexOf(prefijo) === 0 && digitos.length > 10) return '+' + digitos;
    return '+' + prefijo + digitos;
  }

  function dibujar(contenedor) {
    if (contenedor.hasAttribute('data-sf-listo')) return;
    contenedor.setAttribute('data-sf-listo', '');

    var form = el('form', 'sf-form');
    form.setAttribute('novalidate', '');

    if (typeof CONFIG.titulo === 'string' && CONFIG.titulo) {
      var titulo = el('h2', 'sf-titulo');
      titulo.textContent = CONFIG.titulo;
      form.appendChild(titulo);
    }

    function campo(nombreCampo, etiqueta, atributos) {
      var caja = el('div', 'sf-campo');
      var id = idUnico(nombreCampo);
      var label = el('label', 'sf-label');
      label.htmlFor = id;
      label.textContent = etiqueta;
      var input = el('input', 'sf-input');
      input.id = id;
      input.name = nombreCampo;
      input.required = true;
      Object.keys(atributos).forEach(function (k) { input.setAttribute(k, atributos[k]); });
      caja.appendChild(label);
      caja.appendChild(input);
      return { caja: caja, input: input, label: label };
    }

    var cNombre = campo('nombre', etiquetas.nombre || 'Nombre completo', {
      type: 'text', autocomplete: 'name'
    });
    var cEmail = campo('email', etiquetas.email || 'Correo electrónico', {
      type: 'email', autocomplete: 'email', inputmode: 'email'
    });
    form.appendChild(cNombre.caja);
    form.appendChild(cEmail.caja);

    // Teléfono: select de país + número en una fila.
    var cajaTel = el('div', 'sf-campo');
    var idTel = idUnico('telefono');
    var labelTel = el('label', 'sf-label');
    labelTel.htmlFor = idTel;
    labelTel.textContent = etiquetas.telefono || 'WhatsApp';
    cajaTel.appendChild(labelTel);
    var filaTel = el('div', 'sf-telefono');
    var select = null;
    if (paises.length) {
      select = el('select', 'sf-select');
      select.id = idUnico('pais');
      select.name = 'pais';
      select.setAttribute('aria-label', 'País');
      select.setAttribute('autocomplete', 'tel-country-code');
      var elegido = false;
      paises.forEach(function (pais) {
        if (!pais || !pais.prefijo) return;
        var opcion = document.createElement('option');
        opcion.value = String(pais.iso || '');
        opcion.setAttribute('data-prefijo', String(pais.prefijo).replace(/\D/g, ''));
        opcion.textContent = String(pais.nombre || pais.iso || '') + ' (+' + String(pais.prefijo).replace(/\D/g, '') + ')';
        if (!elegido && pais.iso === CONFIG.pais_por_defecto) {
          opcion.selected = true;
          elegido = true;
        }
        select.appendChild(opcion);
      });
      filaTel.appendChild(select);
    }
    var inputTel = el('input', 'sf-input');
    inputTel.id = idTel;
    inputTel.name = 'telefono';
    inputTel.type = 'tel';
    inputTel.required = true;
    inputTel.setAttribute('autocomplete', 'tel-national');
    inputTel.setAttribute('inputmode', 'tel');
    filaTel.appendChild(inputTel);
    cajaTel.appendChild(filaTel);
    form.appendChild(cajaTel);

    // Trampa para bots.
    var trampaCaja = el('div', 'sf-trampa');
    trampaCaja.setAttribute('aria-hidden', 'true');
    var idTrampa = idUnico('website');
    var trampaLabel = document.createElement('label');
    trampaLabel.htmlFor = idTrampa;
    trampaLabel.textContent = 'Sitio web';
    var trampa = document.createElement('input');
    trampa.type = 'text';
    trampa.id = idTrampa;
    trampa.name = 'website';
    trampa.tabIndex = -1;
    trampa.setAttribute('autocomplete', 'off');
    trampaCaja.appendChild(trampaLabel);
    trampaCaja.appendChild(trampa);
    form.appendChild(trampaCaja);

    var aclaracion = armarAclaracion();
    if (aclaracion) form.appendChild(aclaracion);

    var textoBoton = (typeof CONFIG.boton === 'string' && CONFIG.boton) ? CONFIG.boton : 'Enviar';
    var boton = el('button', 'sf-boton');
    boton.type = 'submit';
    boton.textContent = textoBoton;
    form.appendChild(boton);

    var mensaje = el('div', 'sf-mensaje');
    mensaje.setAttribute('role', 'alert');
    mensaje.setAttribute('aria-live', 'polite');
    form.appendChild(mensaje);

    contenedor.appendChild(form);

    var enviando = false;

    function mostrar(texto, tipo) {
      mensaje.className = 'sf-mensaje' + (tipo ? ' sf-' + tipo : '');
      mensaje.textContent = texto || '';
    }

    function marcar(input, invalido) {
      if (invalido) input.setAttribute('aria-invalid', 'true');
      else input.removeAttribute('aria-invalid');
    }

    function habilitar() {
      enviando = false;
      boton.disabled = false;
      boton.textContent = textoBoton;
    }

    function prefijoElegido() {
      if (!select) return '';
      var opcion = select.options[select.selectedIndex];
      return opcion ? (opcion.getAttribute('data-prefijo') || '') : '';
    }

    function validar() {
      var nombre = cNombre.input.value.trim();
      var email = cEmail.input.value.trim();
      var digitos = inputTel.value.replace(/\D/g, '');
      var errores = [
        [cNombre.input, nombre.replace(/\s/g, '').length < 2, MENSAJES.nombre],
        [cEmail.input, !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email), MENSAJES.email],
        [inputTel, digitos.length < 6 || digitos.length > 15, MENSAJES.telefono]
      ];
      var primero = null;
      errores.forEach(function (e) {
        marcar(e[0], e[1]);
        if (e[1] && !primero) primero = e;
      });
      if (primero) {
        mostrar(primero[2], 'error');
        try { primero[0].focus(); } catch (e) { /* nada */ }
        return null;
      }
      return { nombre: nombre, email: email };
    }

    form.addEventListener('submit', function (evento) {
      evento.preventDefault();
      if (enviando) return;
      var datos = validar();
      if (!datos) return;

      var telefono = normalizarTelefono(inputTel.value, prefijoElegido());
      enviando = true;
      boton.disabled = true;
      boton.textContent = MENSAJES.enviando;
      mostrar('', null);

      var cuerpo = {
        nombre: datos.nombre,
        email: datos.email,
        telefono: telefono,
        pagina: location.href,
        referrer: document.referrer || '',
        website: trampa.value || '',
        fbc: leerCookie('_fbc'),
        fbp: leerCookie('_fbp')
      };

      var pedido;
      try {
        pedido = fetch(CONFIG.enviar_url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'omit',
          body: JSON.stringify(cuerpo)
        });
      } catch (e) {
        pedido = Promise.reject(e);
      }

      pedido.then(function (respuesta) {
        return respuesta.json().then(
          function (json) { return { respuesta: respuesta, json: json }; },
          function () { return { respuesta: respuesta, json: null }; }
        );
      }).then(function (r) {
        var estado = r.respuesta.status;
        var json = r.json || {};
        if (r.respuesta.ok) {
          exito(json, datos, telefono);
          return;
        }
        if (estado === 422) {
          var detalle = json && json.detail;
          var texto = (detalle && typeof detalle === 'object' && typeof detalle.mensaje === 'string' && detalle.mensaje)
            ? detalle.mensaje : MENSAJES.datos;
          mostrar(texto, 'error');
        } else if (estado === 429) {
          mostrar(MENSAJES.demasiados, 'error');
        } else {
          mostrar(MENSAJES.red, 'error');
        }
        habilitar();
      }).catch(function () {
        mostrar(MENSAJES.red, 'error');
        habilitar();
      });
    });

    function exito(json, datos, telefono) {
      try {
        contenedor.dispatchEvent(new CustomEvent('setter-form:enviado', {
          bubbles: true,
          detail: { clave: clave, status: json.status }
        }));
      } catch (e) { /* un listener roto no frena el envío */ }

      var sp = window.superpowers;
      if (CONFIG.usar_core !== false && sp && typeof sp.formSubmitted === 'function') {
        try {
          sp.formSubmitted({ full_name: datos.nombre, email: datos.email, phone: telefono });
          mostrar(MENSAJES.listo, 'ok');
          return;
        } catch (e) {
          // Si el core falla, se sigue con la redirección propia.
        }
      }

      var destino = json.gracias_url || json.checkout_url;
      if (typeof destino === 'string' && destino) {
        mostrar(MENSAJES.listo, 'ok');
        location.href = destino;
      } else {
        mostrar(MENSAJES.listo, 'ok');
      }
    }
  }

  function iniciar() {
    inyectarCss();
    var selector = '[data-setter-form="' + clave.replace(/["\\]/g, '\\$&') + '"]';
    var contenedores = document.querySelectorAll(selector);
    if (!contenedores.length) {
      var div = document.createElement('div');
      div.setAttribute('data-setter-form', clave);
      if (scriptActual && scriptActual.parentNode) {
        scriptActual.parentNode.insertBefore(div, scriptActual.nextSibling);
      } else if (document.body) {
        document.body.appendChild(div);
      } else {
        return;
      }
      contenedores = [div];
    }
    Array.prototype.forEach.call(contenedores, dibujar);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', iniciar);
  } else {
    iniciar();
  }
})();
