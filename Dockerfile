FROM python@sha256:e0b3f8d16a5ccfbe602d5c94297e5dbcda2ec256edca2b80fcd44e39e1ff6feb AS python
FROM rust@sha256:4334151ca823b4a52237f3a2d1b16973d6c3c0dd2a7e0ec66a9a8947713d4fa4
COPY --from=python /usr/local /usr/local
COPY --from=verified_inputs / /inputs/
COPY artifacts.json install_tools.py /packet/
RUN ldconfig && python3 /packet/install_tools.py && useradd --create-home --uid 1001 validation
ENV PATH=/opt/toolchains/1.97.0/bin:/opt/tools:/usr/local/bin:/usr/bin:/bin:/usr/local/cargo/bin
ENV PYTHONNOUSERSITE=1 PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1
USER 1001:1001
