
from sklearn.preprocessing import RobustScaler

class AsteroidNormalizer:
    def __init__(self):
        self.scaler = RobustScaler(with_centering=False)

    def fit(self, X_train):
        # X: (B, T, 3)
        positions = X_train[:, :, :2].reshape(-1, 2)
        self.scaler.fit(positions)

    def transform(self, X, Y=None):
        X = X.copy()
        B, T, _ = X.shape

        X[:, :, :2] = self.scaler.transform(
            X[:, :, :2].reshape(-1, 2)
        ).reshape(B, T, 2)

        if Y is not None:
            Y = self.scaler.transform(Y.copy())

        return X, Y

    def inverse_transform(self, Y):
        return self.scaler.inverse_transform(Y)
