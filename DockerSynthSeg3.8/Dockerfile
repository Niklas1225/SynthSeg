#uses cuda11
FROM nvcr.io/nvidia/tensorflow:20.08-tf2-py3

# Change working directory to TensorFlow source path
WORKDIR /opt/tensorflow

# Apply modifications

# Rebuild TensorFlow
RUN ./nvbuild.sh --python3.8 --tf2

# Reset default working directory
WORKDIR /workspace

COPY . /SynthSeg

RUN pip install --upgrade pip && \
    pip install jupyterlab

RUN cd /workspace/SynthSeg
RUN python setup_nvidia.py install
