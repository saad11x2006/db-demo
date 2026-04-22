import streamlit as st
import pandas as pd

# load the data
df = pd.read_csv("titanic.csv")

st.dataframe(df.head(5))
option = st.selectbox("Select column", ("survived", "sex", "class"))

# st.write(option)
selected_count = df[option].value_counts()
st.write(selected_count)
st.bar_chart(selected_count, horizontal=True)

