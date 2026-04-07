import streamlit as st
import numpy as np
import pandas as pd
import psycopg2

# st.connection("sql")

st.snow()

def conn_pukki():
    conn = psycopg2.connect(
        host = st.secrets["db"]["host"],
        port = st.secrets["db"]["port"],
        dbname = st.secrets["db"]["dbname"],
        user = st.secrets["db"]["user"],
        password = st.secrets["db"]["password"],
        sslmode = "require" 
)

conn = psycopg2.connect(
    host = st.secrets["db"]["host"],
    port = st.secrets["db"]["port"],
    dbname = st.secrets["db"]["dbname"],
    user = st.secrets["db"]["user"],
    password = st.secrets["db"]["password"],
    sslmode = "require"
)

df = pd.read_sql("SELECT * FROM table demo", conn)
st.dataframe(df)