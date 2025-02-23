import torch
import torch.nn as nn
import torch.nn.functional as F
from .network import PulseDetectionNet 
from torchinfo import summary

class CrossSiteAttention(nn.Module):
    def __init__(self, hidden_size, num_heads=4):
        super().__init__()
        self.mha = nn.MultiheadAttention(hidden_size, num_heads, batch_first=True)
        self.norm = nn.LayerNorm(hidden_size)
        
    def forward(self, site_features):
        # site_features: list of (N, L, C) tensors
        stacked = torch.stack(site_features, dim=1)  # (N, num_sites, L, C)
        N, num_sites, L, C = stacked.shape
        
        # Reshape to process each timepoint: (N*L, num_sites, C)
        # This way sites can attend to each other at each timepoint
        reshaped = stacked.transpose(1, 2).reshape(N*L, num_sites, C)
        
        # Self attention across sites
        attn_out, _ = self.mha(reshaped, reshaped, reshaped)
        
        # Residual connection and normalization
        attn_out = attn_out + reshaped
        attn_out = self.norm(attn_out)
        
        # Reshape back: (N, L, num_sites, C)
        attn_out = attn_out.reshape(N, L, num_sites, C).transpose(1, 2)
        
        # Split back to list of tensors [(N, L, C), ...]
        return [attn_out[:, i, :, :] for i in range(num_sites)]

class CrossSiteTemporalAttention(nn.Module):
    def __init__(self, hidden_size, num_heads=4):
        super().__init__()
        self.site_attention = nn.MultiheadAttention(hidden_size, num_heads, batch_first=True)
        self.temporal_attention = nn.MultiheadAttention(hidden_size, num_heads, batch_first=True)
        self.norm1 = nn.LayerNorm(hidden_size)
        self.norm2 = nn.LayerNorm(hidden_size)
        
    def forward(self, site_features):
        # site_features: list of (N, L, C) tensors
        stacked = torch.stack(site_features, dim=1)  # (N, num_sites, L, C)
        N, num_sites, L, C = stacked.shape
        
        # 1. Cross-site attention at each timepoint
        reshaped = stacked.transpose(1, 2).reshape(N*L, num_sites, C)
        site_attn, _ = self.site_attention(reshaped, reshaped, reshaped)
        site_attn = self.norm1(site_attn + reshaped)
        
        # Reshape for temporal attention: (N*num_sites, L, C)
        temporal_input = site_attn.reshape(N, L, num_sites, C).transpose(1, 2)
        temporal_input = temporal_input.reshape(N*num_sites, L, C)
        
        # 2. Temporal attention for each site's features
        temporal_attn, _ = self.temporal_attention(temporal_input, temporal_input, temporal_input)
        temporal_attn = self.norm2(temporal_attn + temporal_input)
        
        # Reshape back: (N, num_sites, L, C)
        output = temporal_attn.reshape(N, num_sites, L, C)
        
        # Return list of tensors [(N, L, C), ...]
        return [output[:, i, :, :] for i in range(num_sites)]

# New class for the PTT regression head.
class PTTRegressionHead(nn.Module):
    def __init__(self, in_features, hidden_features=64):
        """
        A simple MLP to regress a single scalar PTT value from concatenated features.
        """
        super().__init__()
        self.fc = nn.Sequential(
            nn.Linear(in_features, hidden_features),
            nn.ReLU(),
            nn.BatchNorm1d(hidden_features), 
            nn.Linear(hidden_features, 1)
        )
        
    def forward(self, x):
        return self.fc(x)

class MultiSitePulseDetectionNet(nn.Module):
    def __init__(self, site_configs, enable_fusion=True, direct_ptt=False, pairs=None):
        """
        Args:
            site_configs: list of dictionaries for each site network.
            enable_fusion: whether to use cross-site fusion.
            direct_ptt: if True, directly output PTT regression values for every possible site pair.
                        (Otherwise, use the original decoding branch for peak probability maps.)
        """
        super().__init__()
        
        self.num_sites = len(site_configs)
        
        # Create site-specific networks.
        self.site_networks = nn.ModuleList([
            PulseDetectionNet(**config) for config in site_configs
        ])
        
        self.site_in_channels = [config['in_channels'] for config in site_configs]
        # Compute the fused feature dimension from bottleneck outputs (bidirectional LSTM).
        lstm_hidden_size = site_configs[0]['lstm_hidden_size'] * 2
        
        self.enable_fusion = enable_fusion
        self.direct_ptt = direct_ptt  # New flag to select regression mode.
        
        if self.enable_fusion:
            self.cross_site_attention = CrossSiteTemporalAttention(
                hidden_size=lstm_hidden_size
            )
            
        if self.direct_ptt:
            if pairs is None:
                # Automatically create all possible site pairs.
                self.all_pairs = []
                for i in range(self.num_sites):
                    for j in range(i+1, self.num_sites):
                        self.all_pairs.append((i, j))
            else:
                self.all_pairs = pairs
            self.fused_feature_dim = lstm_hidden_size  # each site's fused feature dimension
            
            # Create a regression head for each site pair.
            self.ptt_regression_heads = nn.ModuleDict()
            for pair in self.all_pairs:
                key = f'{pair[0]}_{pair[1]}'
                self.ptt_regression_heads[key] = PTTRegressionHead(
                    in_features=2 * self.fused_feature_dim, hidden_features=self.fused_feature_dim
                )
                
    def forward(self, site_inputs):
        """
        Args:
            site_inputs: A tensor with shape (N, num_sites, spatial, temporal)
        """
        # Encode each site's features.
        encoded_features = []
        skip_connections = []
        
        for i, network in enumerate(self.site_networks):
            # Each site input: (N, spatial, temporal) with spatial channels given by self.site_in_channels[i]
            feat, skip = network.encode(site_inputs[:, i, :, :self.site_in_channels[i]])
            # Apply bottleneck processing (e.g. LSTM); output shape: (N, L, C)
            feat = network.bottleneck(feat)
            encoded_features.append(feat)
            skip_connections.append(skip)
            
        # Cross-site fusion.
        if self.enable_fusion:
            fused_features = self.cross_site_attention(encoded_features)
        else:
            fused_features = encoded_features
        
        if self.direct_ptt:
            # --- Direct PTT Regression branch ---
            # Pool the fused features over time so that each site produces one feature vector.
            pooled_features = [feat.mean(dim=1) for feat in fused_features]  # List of (N, C)
            
            ptt_outputs = []
            for pair in self.all_pairs:
                key = f'{pair[0]}_{pair[1]}'
                # Concatenate the two sites' pooled features: (N, 2*C)
                reg_input = torch.cat([pooled_features[pair[0]], pooled_features[pair[1]]], dim=1)
                # Regression head predicts one scalar PTT value for this pair.
                ptt_value = self.ptt_regression_heads[key](reg_input)  # (N, 1)
                ptt_outputs.append(ptt_value)
            # Concatenate outputs along dimension 1 ==> (N, num_pairs) where each column is one PTT regression output.
            return torch.cat(ptt_outputs, dim=1)
        else:
            # --- Original decoding branch (outputs temporal probability maps per site) ---
            outputs = []
            for i, network in enumerate(self.site_networks):
                out = network.decode(fused_features[i], skip_connections[i])
                outputs.append(out)
            return torch.stack(outputs, dim=1)
            # End of original branch.
            
    # The following are helper methods that remain unchanged.
    def load_pretrained(self, paths):
        for network, path in zip(self.site_networks, paths):
            state_dict = torch.load(path)['state_dict']
            new_state_dict = {k.replace('model.', ''): v for k, v in state_dict.items()}
            network.load_state_dict(new_state_dict)
    
    def unfreeze_site(self, site_idx):
        """Unfreeze parameters for a specific site"""
        for param in self.site_networks[site_idx].parameters():
            param.requires_grad = True
            
    def freeze_site(self, site_idx):
        """Freeze parameters for a specific site"""
        for param in self.site_networks[site_idx].parameters():
            param.requires_grad = False
    
    def freeze_all_sites(self):
        for i in range(self.num_sites):
            self.freeze_site(i)

# Testing the direct regression branch:
if __name__ == '__main__':
    in_channels = 21
    seq_len = 5000
    lstm_hidden_size = 64
    site_configs = [
        {
            'in_channels': in_channels,
            'seq_len': seq_len,
            'lstm_hidden_size': lstm_hidden_size
        },
        {
            'in_channels': in_channels*2,
            'seq_len': seq_len,
            'lstm_hidden_size': lstm_hidden_size
        },
        {
            'in_channels': in_channels,
            'seq_len': seq_len,
            'lstm_hidden_size': lstm_hidden_size
        },
    ]
    
    # Set direct_ptt=True to use the regression branch.
    model = MultiSitePulseDetectionNet(site_configs, enable_fusion=True, direct_ptt=True)
    info = summary(model, input_size=(3, 16, seq_len, in_channels), device='cpu')
    inputs = torch.randn(16, 3, seq_len, in_channels)
    outputs = model(inputs)
    print("Output shape (N, num_pairs):", outputs.shape)