import numpy as np
import SimpleITK as sitk
import re
import cv2
import sys
import pandas as pd
import czifile
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

from scipy.spatial.distance import cdist
from scipy.spatial import KDTree
from skimage.draw import disk
import imageio
from skimage.transform import resize

from scipy.ndimage import gaussian_filter, center_of_mass
from scipy.ndimage import binary_dilation, binary_erosion
from stardist.models import StarDist2D, StarDist3D
from csbdeep.utils import normalize
from xml.etree import ElementTree as ET
from skimage.filters import threshold_otsu
from skimage import exposure

from scipy.ndimage import shift

from valis import registration
import time

def open_image(image_path):
    with czifile.CziFile(image_path) as czi:
        image_czi = czi.asarray()
        dic_dim = dict_shape(czi)  
        axes = czi.axes  
        metadata_xml = czi.metadata()
        metadata = ET.fromstring(metadata_xml)
        #save_metadata_to_txt(metadata_xml, '/Users/xavierdekeme/Desktop/metadata.txt')
        channels_dict = channels_dict_def(metadata)

    return image_czi, dic_dim, channels_dict, axes

def save_centers_to_csv(centers, csv_file_path):
    """
    Enregistre les centres des cercles dans un fichier CSV.
    
    Args:
    - centers (dict): Un dictionnaire contenant les centres des cercles.
                      Les clés sont les identifiants des cercles et les valeurs sont des tuples ou listes de coordonnées (x, y).
    - csv_file_path (str): Le chemin vers le fichier CSV à créer.
    """

    # Convertir le dictionnaire des centres en DataFrame pandas
    centers_df = pd.DataFrame.from_dict(centers, orient='index', columns=['Z', 'Y', 'X'])
    
    # Enregistrer le DataFrame dans un fichier CSV
    centers_df.to_csv(csv_file_path, index_label='CircleID')

def save_labels_to_csv(labels_image, csv_file_path):
    # Trouver les labels uniques dans l'image, en ignorant le fond (label 0)
    unique_labels = np.unique(labels_image[labels_image > 0])

    # Initialiser une liste pour stocker les données des labels
    labels_data = []

    for label in unique_labels:
        # Trouver les coordonnées des voxels qui appartiennent à ce label
        z_coords, y_coords, x_coords = np.where(labels_image == label)

        # Pour chaque voxel, ajouter ses coordonnées et son label à la liste
        for z, y, x in zip(z_coords, y_coords, x_coords):
            labels_data.append({"Label": label, "X": x, "Y": y, "Z": z})

    # Convertir la liste en DataFrame pandas
    labels_df = pd.DataFrame(labels_data)
    
    # Enregistrer le DataFrame dans un fichier CSV
    labels_df.to_csv(csv_file_path, index=False)

def save_vol_to_csv(df_volume, csv_file_path):
    """
    Enregistre les volumes des labels dans un fichier CSV.

    Args:
    - df_volume (DataFrame): DataFrame contenant les volumes des labels. 
                             Il doit avoir au moins deux colonnes : 'Label' et 'Volume'.
    - csv_file_path (str): Le chemin vers le fichier CSV à créer.
    """

    # Vérifier si le DataFrame contient les colonnes nécessaires
    if 'Label' not in df_volume.columns or 'Volume' not in df_volume.columns:
        raise ValueError("DataFrame must contain 'Label' and 'Volume' columns")

    # Enregistrer le DataFrame dans un fichier CSV
    df_volume.to_csv(csv_file_path, index=False)

def save_metadata_to_txt(metadata_xml, file_path):
    with open(file_path, 'w') as file:
        file.write(metadata_xml)

#return a dictionary of the czi shape dimensions:
def dict_shape(czi):
    return dict(zip([*czi.axes],czi.shape))

def channels_dict_def(metadata):
    channels = metadata.findall('.//Channel')
    channels_dict = {}
    for chan in channels:
        name = chan.attrib['Name']
        dye_element = chan.find('DyeName')
        if dye_element is not None:
            dye_numbers = re.findall(r'\d+', dye_element.text)
            dye_number = dye_numbers[-1] if dye_numbers else 'Unknown'
            channels_dict[dye_number] = name
    return channels_dict

def czi_slicer(arr,axes,indexes={"S":0, "C":0}):
    ret = arr
    axes = [*axes]
    for k,v in indexes.items():
        index = axes.index(k)
        axes.remove(k)
        ret = ret.take(axis=index, indices=v)
 
    ret_axes = ""
    for i,v in enumerate(ret.shape):
        if v > 1:
            ret_axes+=axes[i]
    return ret.squeeze(),ret_axes

def normalize_channel(channel, min_val=0, max_val=255):
    channel_normalized = exposure.rescale_intensity(channel, out_range=(min_val, max_val))
    return channel_normalized

def get_channel_index(channel_name, channel_dict):
    count = 0
    for keys, values in channel_dict.items():
        if values == channel_name:
            return count
        else:
            count += 1

def isolate_and_normalize_channel(image, dic_dim, channel_dict, TAG, axes, TAG_name):
    channel_name = channel_dict[TAG]
    channel_index = get_channel_index(channel_name, channel_dict)
    if channel_index < 0 or channel_index >= dic_dim['C']:
        raise ValueError("Channel index out of range.")

    image_czi_reduite, axes_restants = czi_slicer(image, axes, indexes={'C': channel_index})
    for i in range(image_czi_reduite.shape[0]):
        imageio.imwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/input_slides/slide_{i}_{TAG_name}.tif', image_czi_reduite[i, :, :])


    #channel_normalized = normalize_channel(image_czi_reduite)
    imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/{TAG_name}.tif', image_czi_reduite.astype(np.uint16))
    return image_czi_reduite.astype(np.uint16)

def reassign_labels(labels_all_slices, df, seuil=10):
    new_labels_all_slices = []
    max_label = 0  # Pour garder une trace du dernier label utilisé

    for z in range(len(labels_all_slices)):
        labels = labels_all_slices[z]
        new_labels = np.zeros_like(labels)
        layer_df = df[df['Layer'] == z]

        # Obtenir les dataframes pour les couches précédente et suivante, si elles existent
        prev_layer_df = df[df['Layer'] == z - 1] if z > 0 else None
        next_layer_df = df[df['Layer'] == z + 1] if z < len(labels_all_slices) - 1 else None

        for idx, row in layer_df.iterrows():
            label = row['Label']
            if label == 0:
                continue
            
            # Trouver les labels similaires dans les couches précédente et suivante
            current_center = np.array([row['Center X'], row['Center Y']])
            similar_label_found = False

            for adjacent_layer_df in [prev_layer_df, next_layer_df]:
                if adjacent_layer_df is not None:
                    adj_centers = adjacent_layer_df[['Center X', 'Center Y']].values
                    if len(adj_centers) > 0:
                        distances = cdist([current_center], adj_centers)
                        min_dist_idx = np.argmin(distances)
                        min_dist = distances[0, min_dist_idx]

                        if min_dist < seuil:
                            similar_label_found = True
                            # Assigner le même label que le label le plus proche dans la couche adjacente
                            adj_label = adjacent_layer_df.iloc[min_dist_idx]['Label']
                            new_labels[labels == label] = new_labels_all_slices[z - 1][labels_all_slices[z - 1] == adj_label][0] if adjacent_layer_df is prev_layer_df else max_label + 1
                            break
            
            # Si aucun label similaire n'a été trouvé dans les couches adjacentes, ne pas réassigner ce label
            if not similar_label_found:
                new_labels[labels == label] = 0
            else:
                max_label += 1

        new_labels_all_slices.append(new_labels)

    return np.stack(new_labels_all_slices, axis=0)

def reassign_labels_2layers(labels_all_slices, df, seuil=25):
    new_labels_all_slices = np.zeros_like(labels_all_slices)  # Créer un tableau de zéros de la même forme que labels_all_slices

    for z in range(len(labels_all_slices)):
        labels = labels_all_slices[z]  # Labels de la couche actuelle
        new_labels = np.zeros_like(labels)  # Initialiser new_labels pour la couche actuelle
        layer_df = df[df['Layer'] == z]

        for idx, row in layer_df.iterrows():
            label = row['Label']
            if label == 0:
                continue

            current_center = np.array([row['Center X'], row['Center Y']])
            similar_label_found = False

            # Examiner les couches -2, -1, +1 et +2
            for offset in [-2, -1, 1, 2]:
                if 0 <= z + offset < len(labels_all_slices): 
                    adjacent_layer_df = df[df['Layer'] == z + offset]

                    if not adjacent_layer_df.empty:
                        adj_centers = adjacent_layer_df[['Center X', 'Center Y']].values
                        distances = cdist([current_center], adj_centers)
                        min_dist_idx = np.argmin(distances)
                        min_dist = distances[0, min_dist_idx]

                        if min_dist < seuil:
                            similar_label_found = True
                            adj_label = adjacent_layer_df.iloc[min_dist_idx]['Label']
                            new_labels[labels == label] = adj_label  # Assigner le label trouvé dans la couche adjacente
                            break

            if not similar_label_found:
                new_labels[labels == label] = label  # Conserver le label original si aucun label similaire n'est trouvé

        new_labels_all_slices[z] = new_labels  # Assigner les nouveaux labels à la couche actuelle

    return new_labels_all_slices



def find_centers_of_labels_in_3d(labels_3d):
    unique_labels = np.unique(labels_3d)
    centers = {}
    
    for label in unique_labels:
        if label == 0:  # ignore background
            continue
        # Trouver le centre de masse pour le label actuel
        center = center_of_mass(labels_3d == label)
        centers[label] = center

    return centers

def normalize_centers(labels_3d):
    unique_labels = np.unique(labels_3d)
    centers = {}
    
    # Obtenez les dimensions de l'image
    depth, height, width = labels_3d.shape
    
    for label in unique_labels:
        if label == 0:  # ignore background
            continue
        
        # Trouver le centre de masse pour le label actuel
        center = center_of_mass(labels_3d == label)
        
        # Normaliser les coordonnées du centre par rapport aux dimensions de l'image ()
        normalized_center = (center[0] / depth, center[1] / height, center[2] / width)
        
        centers[label] = normalized_center

    return centers

def plot_3d_centers(centers):
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')

    # Séparez les coordonnées z, y, x
    zs = [center[0] for center in centers.values()]
    ys = [center[1] for center in centers.values()]
    xs = [center[2] for center in centers.values()]

    ax.scatter(xs, ys, zs)

    ax.set_xlabel('X Coordinate')
    ax.set_ylabel('Y Coordinate')
    ax.set_zlabel('Z Coordinate')

    plt.show()

def plot_3d_centers_all(centers_TAG1, centers_TAG2, common_centers):
    fig = plt.figure()
    ax = fig.add_subplot(111, projection='3d')

    # Tracer les centres de TAG1
    zs_TAG1 = [center[0] for center in centers_TAG1.values()]
    ys_TAG1 = [center[1] for center in centers_TAG1.values()]
    xs_TAG1 = [center[2] for center in centers_TAG1.values()]
    ax.scatter(xs_TAG1, ys_TAG1, zs_TAG1, color='r', label='TAG1')

    # Tracer les centres de TAG2
    zs_TAG2 = [center[0] for center in centers_TAG2.values()]
    ys_TAG2 = [center[1] for center in centers_TAG2.values()]
    xs_TAG2 = [center[2] for center in centers_TAG2.values()]
    ax.scatter(xs_TAG2, ys_TAG2, zs_TAG2, color='b', label='TAG2')

    # Tracer les centres communs
    zs_common = [center[0] for center in common_centers.values()]
    ys_common = [center[1] for center in common_centers.values()]
    xs_common = [center[2] for center in common_centers.values()]
    ax.scatter(xs_common, ys_common, zs_common, color='g', label='Commun')

    ax.set_xlabel('X Coordinate')
    ax.set_ylabel('Y Coordinate')
    ax.set_zlabel('Z Coordinate')
    ax.legend()

    plt.show()

def plot_label_areas(labels_3d):
    depth = labels_3d.shape[0]
    
    rows = int(np.ceil(np.sqrt(depth)))
    cols = int(np.ceil(depth / rows))

    fig, axes = plt.subplots(rows, cols, figsize=(20, 10))  # Ajustez la taille de la figure selon vos besoins
    axes_flat = axes.flatten()

    for z in range(depth):
        layer = labels_3d[z, :, :]
        unique_labels, counts = np.unique(layer, return_counts=True)
        
        valid_indices = unique_labels != 0
        unique_labels = unique_labels[valid_indices]
        counts = counts[valid_indices]

        # Tri des labels et des aires
        sorted_areas_indices = np.argsort(counts)
        sorted_labels = unique_labels[sorted_areas_indices]
        sorted_areas = counts[sorted_areas_indices]

        # Création du graphique en bâtonnets pour chaque couche avec les labels triés
        axes_flat[z].bar(range(len(sorted_labels)), sorted_areas, color='skyblue', tick_label=sorted_labels)
        axes_flat[z].set_title(f'Layer {z + 1}')
        axes_flat[z].set_xlabel('Sorted Label Number')
        axes_flat[z].set_ylabel('Area (pixels)')

        # Rotation des étiquettes sur l'axe x pour une meilleure lisibilité
        for label in axes_flat[z].get_xticklabels():
            label.set_rotation(45)
            label.set_ha('right')
    
    # Masquer les axes inutilisés
    for ax in axes_flat[depth:]:
        ax.axis('off')
    
    plt.tight_layout()
    plt.show()

def do_segmentation(model, dic_dim, isolate_image, SURFACE_THRESHOLD_SUP, SURFACE_THRESHOLD_INF):
    labels_all_slices = []
    data = []
    gaussian_kernel_size = (5, 5) 
    gaussian_sigma_x = 0

    for z in range(dic_dim['Z']):
        img_slice = isolate_image[z, :, :]
        img_slice = cv2.GaussianBlur(img_slice, gaussian_kernel_size, gaussian_sigma_x)
        img_slice = normalize(img_slice, 1, 99.8)
        labels, details = model.predict_instances(img_slice)
        unique_labels = np.unique(labels)

        labels_to_remove = []
        for label_num in unique_labels:
            if label_num == 0: 
                continue

            instance_mask = labels == label_num
            label_surface = np.sum(instance_mask)

            if label_surface > SURFACE_THRESHOLD_SUP or label_surface < SURFACE_THRESHOLD_INF:
                labels[instance_mask] = 0
                labels_to_remove.append(label_num)

        unique_labels = np.array([label for label in unique_labels if label not in labels_to_remove])
        for label in unique_labels:
            if label == 0:  
                continue
            center = center_of_mass(labels == label)
            data.append({'Layer': z, 'Label': label, 'Center X': center[0], 'Center Y': center[1]})

        labels_all_slices.append(labels)

    return labels_all_slices, data

def update_labels_image(labels_image, labels_to_keep):
    updated_labels_image = np.zeros_like(labels_image)
    for label in labels_to_keep:
        updated_labels_image[labels_image == label] = label
    return updated_labels_image

def keep_regions_with_all_tags(centers_TAG1, centers_TAG2, centers_TAG3, distance_threshold=0.1, weights=(1, 1, 1)):
    updated_centers_TAG1 = {}
    updated_centers_TAG2 = {}
    updated_centers_TAG3 = {}

    # Appliquer les poids aux coordonnées des centres
    def apply_weights(coordinates, weights):
        return np.multiply(coordinates, weights)

    coordinates_TAG1_weighted = apply_weights(np.array(list(centers_TAG1.values())), weights)
    coordinates_TAG2_weighted = apply_weights(np.array(list(centers_TAG2.values())), weights)
    coordinates_TAG3_weighted = apply_weights(np.array(list(centers_TAG3.values())), weights)

    for tag_dict, tag_coords_weighted, other_coords1_weighted, other_coords2_weighted, updated_dict in [
        (centers_TAG1, coordinates_TAG1_weighted, coordinates_TAG2_weighted, coordinates_TAG3_weighted, updated_centers_TAG1),
        (centers_TAG2, coordinates_TAG2_weighted, coordinates_TAG1_weighted, coordinates_TAG3_weighted, updated_centers_TAG2),
        (centers_TAG3, coordinates_TAG3_weighted, coordinates_TAG1_weighted, coordinates_TAG2_weighted, updated_centers_TAG3)]:

        for label, point in tag_dict.items():
            point_weighted = np.multiply(point, weights)

            distances_to_other1 = cdist([point_weighted], other_coords1_weighted)
            distances_to_other2 = cdist([point_weighted], other_coords2_weighted)

            nearest_other1_idx = np.argmin(distances_to_other1) if np.any(distances_to_other1 <= distance_threshold) else None
            nearest_other2_idx = np.argmin(distances_to_other2) if np.any(distances_to_other2 <= distance_threshold) else None

            if nearest_other1_idx is not None and nearest_other2_idx is not None:
                updated_dict[label] = point
                if tag_dict is centers_TAG1:
                    updated_centers_TAG2[list(centers_TAG2.keys())[nearest_other1_idx]] = other_coords1_weighted[nearest_other1_idx] / weights
                    updated_centers_TAG3[list(centers_TAG3.keys())[nearest_other2_idx]] = other_coords2_weighted[nearest_other2_idx] / weights
                elif tag_dict is centers_TAG2:
                    updated_centers_TAG1[list(centers_TAG1.keys())[nearest_other1_idx]] = other_coords1_weighted[nearest_other1_idx] / weights
                    updated_centers_TAG3[list(centers_TAG3.keys())[nearest_other2_idx]] = other_coords2_weighted[nearest_other2_idx] / weights
                else:  # tag_dict is centers_TAG3
                    updated_centers_TAG1[list(centers_TAG1.keys())[nearest_other1_idx]] = other_coords1_weighted[nearest_other1_idx] / weights
                    updated_centers_TAG2[list(centers_TAG2.keys())[nearest_other2_idx]] = other_coords2_weighted[nearest_other2_idx] / weights

    return updated_centers_TAG1, updated_centers_TAG2, updated_centers_TAG3

def calculate_shift(pairs):
    """
    Calcule le déplacement moyen à partir des paires de points.

    Args:
    - pairs (list): Liste de tuples contenant les paires de points (point_img1, point_img2).

    Returns:
    - shift_values (tuple): Le déplacement moyen en x et y.
    """
    shifts = [np.array(point_img2) - np.array(point_img1) for point_img1, point_img2 in pairs]
    shift_values = np.mean(shifts, axis=0)
    return shift_values

def shift_image(image, shift_values):
    """
    Déplace l'image en fonction des valeurs de déplacement.

    Args:
    - image (ndarray): Image à déplacer.
    - shift_values (tuple): Valeurs de déplacement en x et y.

    Returns:
    - shifted_image (ndarray): Image déplacée.
    """
    shifted_image = np.zeros_like(image)

    # Application du déplacement à chaque couche z de l'image
    for z in range(image.shape[0]):
        shifted_image[z, :, :] = shift(image[z, :, :], shift_values)

    return shifted_image

def adjust_labels_and_calculate_volumes(labels_all_slices):
    adjusted_labels_all_slices = np.copy(labels_all_slices)
    labels_area = {}

    for z in range(labels_all_slices.shape[0]):
        for label in np.unique(labels_all_slices[z, :, :]):
            if label == 0:
                continue
            
            current_label_mask = labels_all_slices[z] == label
            current_label_area = np.sum(current_label_mask)

            if f"Label {label}" not in labels_area:
                labels_area[f"Label {label}"] = {z: current_label_area}
            else:
                labels_area[f"Label {label}"][z] = current_label_area

    volumes = {label: sum(areas.values()) for label, areas in labels_area.items()}
    df_volumes = pd.DataFrame(list(volumes.items()), columns=['Label', 'Volume'])
    
    for label, layers in labels_area.items():
        layer_numbers = sorted(layers.keys())
        for i, z in enumerate(layer_numbers):
            current_area = layers[z]
            if i > 0 and i < len(layer_numbers)-1:
                prev_area = layers[layer_numbers[i - 1]]
                next_area = layers[layer_numbers[i + 1]]
                target_area = (prev_area + next_area) / 2 

                if abs(current_area - target_area) > current_area * 0.25:
                    current_label_mask = labels_all_slices[z] == int(label.split(" ")[-1])
                    # Supprimer la segmentation problématique en réinitialisant cette région dans l'image des labels ajustés
                    adjusted_labels_all_slices[z][current_label_mask] = 0

                    # Remplacer par une segmentation "normale"
                    # Cette étape dépend de ce que vous avez disponible ou de ce que vous considérez comme une segmentation "normale"
                    # Par exemple, utiliser la segmentation de la couche précédente si z > 0
                    if z > 0:
                        # Utiliser la segmentation de la couche précédente
                        previous_label_mask = labels_all_slices[z - 1] == int(label.split(" ")[-1])
                        adjusted_labels_all_slices[z][previous_label_mask] = int(label.split(" ")[-1])
                    elif z < len(labels_all_slices) - 1:
                        # Ou utiliser la segmentation de la couche suivante si z est la première couche
                        next_label_mask = labels_all_slices[z + 1] == int(label.split(" ")[-1])
                        adjusted_labels_all_slices[z][next_label_mask] = int(label.split(" ")[-1])

    return adjusted_labels_all_slices, df_volumes


IMAGE_NAME = 'billes 40X-1'
IMAGE_NAME_2 = 'billes 40X-2'
DAPI = 'Unknown'
PHALLOIDINE = '488'



image_path = f'/Volumes/LaCie/Mémoire/Outil2.0/server/project/billes/billes 40X-1.czi'
image_czi_1, dic_dim_1, channels_dict_1, axes_1 = open_image(image_path)
image_path_2 = f'/Volumes/LaCie/Mémoire/Outil2.0/server/project/billes/billes 40X-2.czi'
image_czi_2, dic_dim_2, channels_dict_2, axes_2 = open_image(image_path_2)


DAPI_img1 = isolate_and_normalize_channel(image_czi_1, dic_dim_1, channels_dict_1, DAPI, axes_1, "DAPI_IMG1")
#PHALLO_img1 = isolate_and_normalize_channel(image_czi_1, dic_dim_1, channels_dict_1, PHALLOIDINE, axes_1, "PHALLO_IMG1")
DAPI_img2 = isolate_and_normalize_channel(image_czi_2, dic_dim_2, channels_dict_2, DAPI, axes_2, "DAPI_IMG2")
#PHALLO_img2 = isolate_and_normalize_channel(image_czi_2, dic_dim_2, channels_dict_2, PHALLOIDINE, axes_2, "PHALLO_IMG2")


# slide_src_dir = "/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/input_slides"
# results_dst_dir = "/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/result"

# # Create a Valis object and use it to register the slides in slide_src_dir
# start = time.time()
# registrar = registration.Valis(slide_src_dir, results_dst_dir)
# rigid_registrar, non_rigid_registrar, error_df = registrar.register()
# stop = time.time()
# elapsed = stop - start
# print(f"regisration time is {elapsed/60} minutes")


# # Check results in registered_slide_dst_dir. If they look good, export the registered slides
# registered_slide_dst_dir = os.path.join("/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/result_slide", registrar.name)
# start = time.time()
# registrar.warp_and_save_slides(registered_slide_dst_dir)
# stop = time.time()
# elapsed = stop - start
# print(f"saving {registrar.size} slides took {elapsed/60} minutes")


# # Shutdown the JVM
# registration.kill_jvm()



# Charger le modèle pré-entraîné StarDist 2D
model = StarDist2D.from_pretrained('2D_versatile_fluo')

# Initialisation d'un dictionnaire pour stocker les informations par TAG
tags_data = {}

# Liste des informations des TAGs
tags_info = [
    {"tag_img": DAPI_img1, "thresholds": (10000, 250), "tag_name": "DAPI_IMG1"}
]

tags_info_2 = [
    {"tag_img": DAPI_img2, "thresholds": (10000, 250), "tag_name": "DAPI_IMG2"}
]



for tag_info in tags_info:
    labels_all_slices, data = do_segmentation(model, dic_dim_1, tag_info["tag_img"], *tag_info["thresholds"])
    labels_all_slices = np.stack(labels_all_slices, axis=0)
    df = pd.DataFrame(data)

    labels = reassign_labels(labels_all_slices, df)
    adjusted_labels, df_volumes = adjust_labels_and_calculate_volumes(labels)
    imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/label_new_{tag_info["tag_name"]}.tif', labels)
    imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/label_new_adj_{tag_info["tag_name"]}.tif', adjusted_labels)

    centers = find_centers_of_labels_in_3d(adjusted_labels)
    centers_norm = normalize_centers(adjusted_labels)

    tags_data[tag_info["tag_name"]] = {
        "labels": labels,
        "centers": centers,
        "centers_norm": centers_norm,
        "df_volume": df_volumes,
        "dataframe": df,
    }


for tag_info in tags_info_2:
    labels_all_slices, data = do_segmentation(model, dic_dim_2, tag_info["tag_img"], *tag_info["thresholds"])
    labels_all_slices = np.stack(labels_all_slices, axis=0)
    df = pd.DataFrame(data)

    labels = reassign_labels(labels_all_slices, df)
    adjusted_labels, df_volumes = adjust_labels_and_calculate_volumes(labels)

    imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/label_new_{tag_info["tag_name"]}.tif', labels)
    imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/billes/label_new_adj_{tag_info["tag_name"]}.tif', adjusted_labels)

    centers = find_centers_of_labels_in_3d(adjusted_labels)
    centers_norm = normalize_centers(adjusted_labels)

    tags_data[tag_info["tag_name"]] = {
        "labels": labels,
        "centers": centers,
        "centers_norm": centers_norm,
        "df_volume": df_volumes,
        "dataframe": df,
    }

# tag1_centers_norm = tags_data["DAPI_IMG1"]["centers_norm"]
# tag2_centers_norm = tags_data["DAPI_IMG2"]["centers_norm"]

# tag1_labels = tags_data["DAPI_IMG1"]["labels"]
# tag2_labels = tags_data["DAPI_IMG2"]["labels"]

# tag1_vol = tags_data["DAPI_IMG1"]["df_volume"]
# tag2_vol = tags_data["DAPI_IMG2"]["df_volume"]


# index = [1, 2]
# for i in index:
#     center_norm_file_path = f"/Users/xavierdekeme/Desktop/Data/CentreNorm/centers_billes{i}.csv"
#     label_file_path = f"/Users/xavierdekeme/Desktop/Data/Label/label_billes{i}.csv"
#     volume_file_path = f"/Users/xavierdekeme/Desktop/Data/Volume/volume_billes{i}.csv"
    
#     center_norm_data = tags_data[f"DAPI_IMG{i}"]["centers_norm"]
#     label_data = tags_data[f"DAPI_IMG{i}"]["labels"]
#     vol_data = tags_data[f"DAPI_IMG{i}"]["df_volume"]

#     save_centers_to_csv(center_norm_data, center_norm_file_path)
#     save_labels_to_csv(label_data, label_file_path)
#     save_vol_to_csv(vol_data, volume_file_path)
    












# #FIND POPULATION
# update_center1, update_center2, update_center7  = keep_regions_with_all_tags(tag1_centers_norm, tag2_centers_norm, tag7_centers_norm, distance_threshold=0.075, weights=(1, 1, 0.25))

# print(update_center1)
# print(update_center2)
# print(update_center7)

# updated_labels_images = update_labels_image(tags_data["TAG1"]["labels"], update_center1)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG1_update_pop127.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG2"]["labels"], update_center2)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG2_update_pop127.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG7"]["labels"], update_center7)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG7_update_pop127.tif', updated_labels_images)



# update_center1, update_center2, update_center3  = keep_regions_with_all_tags(tag1_centers_norm, tag2_centers_norm, tag3_centers_norm, distance_threshold=0.075, weights=(1, 1, 0.25))

# print(update_center1)
# print(update_center2)
# print(update_center3)

# updated_labels_images = update_labels_image(tags_data["TAG1"]["labels"], update_center1)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG1_update_pop123.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG2"]["labels"], update_center2)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG2_update_pop123.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG3"]["labels"], update_center3)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG3_update_pop123.tif', updated_labels_images)


# update_center1, update_center5, update_center7  = keep_regions_with_all_tags(tag1_centers_norm, tag5_centers_norm, tag7_centers_norm, distance_threshold=0.075, weights=(1, 1, 0.25))

# print(update_center1)
# print(update_center5)
# print(update_center7)

# updated_labels_images = update_labels_image(tags_data["TAG1"]["labels"], update_center1)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG1_update_pop157.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG5"]["labels"], update_center5)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG5_update_pop157.tif', updated_labels_images)
# updated_labels_images = update_labels_image(tags_data["TAG7"]["labels"], update_center7)
# imageio.volwrite(f'/Users/xavierdekeme/Desktop/ULB-MA2/Memoire/Outil2.0/server/project/New/label_TAG7_update_pop157.tif', updated_labels_images)


