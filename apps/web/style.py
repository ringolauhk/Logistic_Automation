"""Compact stylesheet for the pilot web UI (M9.1).

Pure presentation: selectors target Streamlit's stable semantic hooks
(element data-testids and public element-container classes such as
``.stButton``), never generated emotion class names. Contains no data,
paths, or secrets - a test asserts that.
"""

# Dense desktop layout: smaller type scale, tighter vertical rhythm, compact
# controls. Body text stays >= ~0.85rem for readability.
COMPACT_CSS = """
<style>
/* page frame: trim outer padding, keep wide layout usable */
[data-testid="stMainBlockContainer"] {
    padding-top: 1.6rem;
    padding-bottom: 1rem;
    padding-left: 2.5rem;
    padding-right: 2.5rem;
}
/* tighter vertical rhythm between elements */
[data-testid="stVerticalBlock"] { gap: 0.45rem; }
[data-testid="stElementContainer"] { margin-bottom: 0; }

/* type scale: compact headings, readable body */
h1 { font-size: 1.45rem !important; padding-bottom: 0.2rem !important; }
h2 { font-size: 1.05rem !important; padding-top: 0.4rem !important;
     padding-bottom: 0.15rem !important; }
h3 { font-size: 0.95rem !important; padding-bottom: 0.1rem !important; }
[data-testid="stMarkdownContainer"] p { font-size: 0.88rem; }
[data-testid="stCaptionContainer"] p { font-size: 0.76rem; }

/* alerts (privacy notice, warnings, errors) */
[data-testid="stAlert"] { padding: 0.45rem 0.7rem; }
[data-testid="stAlert"] p { font-size: 0.8rem; }

/* compact controls */
.stButton button, .stDownloadButton button {
    padding: 0.2rem 0.8rem;
    font-size: 0.85rem;
    min-height: 1.9rem;
}
.stCheckbox { min-height: 1.4rem; }
.stCheckbox p { font-size: 0.85rem; }
.stNumberInput input, .stTextInput input {
    padding: 0.2rem 0.5rem;
    font-size: 0.85rem;
}
.stNumberInput div[data-baseweb], .stTextInput div[data-baseweb] {
    min-height: 1.9rem;
}
[data-testid="stWidgetLabel"] p { font-size: 0.8rem; }

/* file uploader: shallow dropzone */
[data-testid="stFileUploaderDropzone"] {
    padding: 0.4rem 0.8rem;
    min-height: 2.6rem;
}
[data-testid="stFileUploaderDropzone"] div { font-size: 0.8rem; }

/* tables and metrics */
[data-testid="stTable"] th, [data-testid="stTable"] td {
    font-size: 0.8rem;
    padding: 0.2rem 0.55rem;
}
[data-testid="stMetric"] { padding: 0.1rem 0; }
[data-testid="stMetricLabel"] p { font-size: 0.72rem; }
[data-testid="stMetricValue"] { font-size: 1.15rem; }

/* expander (advanced settings) and progress rows */
[data-testid="stExpander"] summary { padding: 0.3rem 0.7rem; }
[data-testid="stExpander"] summary p { font-size: 0.85rem; }
[data-testid="stProgress"] { margin: 0.15rem 0; }
[data-testid="stProgress"] p { font-size: 0.78rem; }
</style>
"""
