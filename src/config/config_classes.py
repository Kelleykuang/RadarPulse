from dataclasses import dataclass
from typing import List
from pathlib import Path

@dataclass
class DataConfig:
    data_path: str
    position: str
    fs: int
    sample_len: int
    overlap: float
    intra_session_split_ratio: float
    n_channels: int
    norm_2d: bool
    
    def validate(self):
        assert 0 < self.overlap < 1
        assert Path(self.data_path).exists()
        assert self.fs > 0

@dataclass
class TrainingConfig:
    batch_size: int
    num_workers: int
    learning_rate: float
    max_epochs: int
    weight_decay: float
    seed: int

@dataclass
class SchedulerConfig:
    type: str
    T_max: int
    T_mult: int
    min_lr: float

@dataclass
class NetworkConfig:
    seq_len: int
    reduce_channels: int
    hidden_channels: List[int]
    kernel_size: int
    use_lstm: bool
    lstm_hidden_size: int
    lstm_num_layers: int
    dropout: float

@dataclass
class LossConfig:
    seq_len: int
    sigma: float
    min_peak_distance: int
    max_peak_distance: int
    distance_weight: float
    count_weight: float

@dataclass
class Config:
    data: DataConfig
    training: TrainingConfig
    scheduler: SchedulerConfig
    network: NetworkConfig
    loss: LossConfig