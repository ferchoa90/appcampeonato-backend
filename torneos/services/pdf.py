"""PDF del calendario completo de un campeonato (todas las fechas, con horarios y descansos)."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from torneos.models import Partido

DIAS = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom']
MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic']
AZUL = colors.HexColor('#405189')
GRIS = colors.HexColor('#878a99')
BORDE = colors.HexColor('#e9ebec')


def _dia(d):
    return f'{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} {d.year}'


def _estilos():
    base = getSampleStyleSheet()['Normal']
    return {
        'titulo': ParagraphStyle('t', parent=base, fontName='Helvetica-Bold', fontSize=18, leading=22, textColor=AZUL),
        'sub': ParagraphStyle('s', parent=base, fontSize=10, leading=13, textColor=GRIS),
        'fecha': ParagraphStyle('f', parent=base, fontName='Helvetica-Bold', fontSize=11, leading=14, textColor=AZUL,
                                spaceBefore=10, spaceAfter=4),
        'celda': ParagraphStyle('c', parent=base, fontSize=8.5, leading=10.5),
        'der': ParagraphStyle('d', parent=base, fontSize=8.5, leading=10.5, alignment=2),
        'centro': ParagraphStyle('m', parent=base, fontName='Helvetica-Bold', fontSize=9, leading=11, alignment=1),
        'peq': ParagraphStyle('p', parent=base, fontSize=7.5, leading=9.5, textColor=GRIS),
        'descansa': ParagraphStyle('x', parent=base, fontSize=8.5, leading=11, textColor=GRIS, spaceBefore=3),
    }


def _cuando(partido):
    if not partido.fecha_hora:
        return 'Por definir'
    d = timezone.localtime(partido.fecha_hora)
    return f'{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} · {d:%H:%M}'


def _marcador(partido):
    if partido.estado in (Partido.Estado.FINALIZADO, Partido.Estado.INICIADO, Partido.Estado.ENTRETIEMPO,
                          Partido.Estado.SEGUNDO_TIEMPO):
        texto = f'{partido.goles_local} - {partido.goles_visitante}'
        if partido.penales_local is not None and partido.penales_visitante is not None:
            texto += f'<br/><font size="7">({partido.penales_local}-{partido.penales_visitante} pen.)</font>'
        return texto
    return 'vs'


def _observacion(partido):
    partes = []
    if partido.grupo_id:
        partes.append(escape(partido.grupo.nombre if partido.grupo else ''))
    if partido.estadio:
        partes.append(escape(partido.estadio))
    estado = {
        Partido.Estado.FINALIZADO: 'Finalizado', Partido.Estado.INICIADO: '1.er tiempo',
        Partido.Estado.ENTRETIEMPO: 'Entretiempo', Partido.Estado.SEGUNDO_TIEMPO: '2.º tiempo',
    }.get(partido.estado)
    if estado:
        partes.append(estado)
    if partido.estado == Partido.Estado.POSTERGADO:
        partes.append(f'<font color="#f06548">Postergado: {escape(partido.motivo_postergacion)}</font>')
    elif partido.fecha_original:
        partes.append(f'Reprogramado (antes: {_dia(timezone.localtime(partido.fecha_original))})')
    return '<br/>'.join(p for p in partes if p)


LOGO_GOALSCORE = Path(__file__).resolve().parent.parent / 'assets' / 'logo-goalscore.png'
PIE = 'Generado por GoalScore, AcepSistemas todos los derechos reservados.'


def _marco(canvas, doc):
    """Cabecera (logo de GoalScore) y pie de página en cada hoja."""
    ancho_pag, alto_pag = A4
    canvas.saveState()
    try:  # el PDF se genera igual si el logo no está
        ancho, alto = ImageReader(str(LOGO_GOALSCORE)).getSize()
        alto_logo = 0.9 * cm
        ancho_logo = ancho * alto_logo / alto
        canvas.drawImage(str(LOGO_GOALSCORE), ancho_pag - 2 * cm - ancho_logo, alto_pag - 1.2 * cm - alto_logo,
                         width=ancho_logo, height=alto_logo, mask='auto')
    except Exception:
        pass
    canvas.setStrokeColor(BORDE)
    canvas.setLineWidth(0.6)
    canvas.line(2 * cm, alto_pag - 2.1 * cm, ancho_pag - 2 * cm, alto_pag - 2.1 * cm)   # bajo la cabecera
    canvas.line(2 * cm, 1.6 * cm, ancho_pag - 2 * cm, 1.6 * cm)                         # sobre el pie
    canvas.setFont('Helvetica', 7.5)
    canvas.setFillColor(GRIS)
    canvas.drawString(2 * cm, 1.05 * cm, PIE)
    canvas.drawRightString(ancho_pag - 2 * cm, 1.05 * cm, f'Página {doc.page}')
    canvas.restoreState()


def _logo(campeonato, alto_max=2.4 * cm, ancho_max=5 * cm):
    """Imagen del logo del campeonato ajustada a una caja, conservando la proporción.

    Devuelve None si no hay logo o no se puede leer (el PDF se genera igual, sin él).
    """
    if not campeonato.logo:
        return None
    try:
        ruta = campeonato.logo.path
        ancho, alto = ImageReader(ruta).getSize()
        escala = min(ancho_max / ancho, alto_max / alto)
        return Image(ruta, width=ancho * escala, height=alto * escala, mask='auto')
    except Exception:  # archivo ausente o dañado
        return None


def calendario_pdf(campeonato):
    """Devuelve los bytes del PDF con el calendario de todas las fechas del campeonato."""
    est = _estilos()
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.7 * cm,
                            bottomMargin=2.2 * cm, title=f'Calendario - {campeonato.nombre}',
                            author='GoalScore · AcepSistemas')
    ancho = [3.2 * cm, 4.3 * cm, 1.6 * cm, 4.3 * cm, 3.6 * cm]

    detalle = ['Calendario completo']
    if campeonato.partidos.exists():
        detalle.append(campeonato.get_modalidad_display())
    detalle.append(f'Generado el {_dia(timezone.localdate())}')
    textos = [Paragraph(escape(campeonato.nombre) + (f' {escape(campeonato.temporada)}' if campeonato.temporada else ''),
                        est['titulo']),
              Paragraph(' · '.join(detalle), est['sub'])]
    logo = _logo(campeonato)
    if logo:
        encabezado = Table([[logo, textos]], colWidths=[logo.drawWidth + 0.5 * cm, 17 * cm - logo.drawWidth - 0.5 * cm])
        encabezado.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'), ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0), ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        historia = [encabezado, Spacer(1, 6)]
    else:
        historia = textos + [Spacer(1, 6)]

    fechas = campeonato.fechas.prefetch_related(
        'partidos__local', 'partidos__visitante', 'partidos__grupo', 'descansos__equipo').order_by('numero')
    if not fechas:
        historia.append(Paragraph('El calendario aún no fue generado.', est['sub']))
    for fecha in fechas:
        titulo = escape(fecha.nombre or f'Fecha {fecha.numero}')
        if fecha.fecha_programada:
            titulo += f' — {_dia(fecha.fecha_programada)}'
        bloque = [Paragraph(titulo, est['fecha'])]
        peq_der = ParagraphStyle('pd', parent=est['peq'], alignment=2)
        filas = [[Paragraph('Día y hora', est['peq']), Paragraph('Local', peq_der), '',
                  Paragraph('Visitante', est['peq']), Paragraph('Detalle', est['peq'])]]
        for p in sorted(fecha.partidos.all(), key=lambda x: (x.fecha_hora is None, x.fecha_hora, x.id)):
            filas.append([
                Paragraph(escape(_cuando(p)), est['celda']),
                Paragraph(escape(p.local.nombre), est['der']),
                Paragraph(_marcador(p), est['centro']),
                Paragraph(escape(p.visitante.nombre), est['celda']),
                Paragraph(_observacion(p), est['peq']),
            ])
        tabla = Table(filas, colWidths=ancho, repeatRows=1)
        tabla.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LINEBELOW', (0, 0), (-1, -1), 0.4, BORDE),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f3f6f9')),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        bloque.append(tabla)
        descansan = [d.equipo.nombre for d in fecha.descansos.all()]
        if descansan:
            bloque.append(Paragraph('Descansa: ' + escape(', '.join(descansan)), est['descansa']))
        historia.append(KeepTogether(bloque))

    doc.build(historia, onFirstPage=_marco, onLaterPages=_marco)
    return buf.getvalue()
